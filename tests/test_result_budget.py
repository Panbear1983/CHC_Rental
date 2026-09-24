"""The per-result spend gate.

The Apify actor bills per RESULT, so the results limit is the bill. These tests
pin the arithmetic that decides it, and the two directions it must fail in:
never overspend the plan cap (that returns 403 on every run and stops the whole
product), and never take the daily scrape down just because the usage endpoint
was unreadable.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date

import pytest

from chc_rental.cost import ResultBudget, plan_result_budget, read_result_budget
from chc_rental.models import Settings
from chc_rental.sources import configured_adapters
from chc_rental.sources.base import SourceQuery, SourceUnavailableError
from chc_rental.sources.zillow import rental_search_url

TODAY = date(2026, 8, 29)
CYCLE_END = "2026-09-20T23:59:59.999Z"  # 23 days left, today included


def settings(**overrides) -> Settings:
    base = dict(
        zillow_enabled=True,
        zillow_results_limit=60,
        zillow_price_per_result_usd=0.0023,
        zillow_budget_target_share=0.85,
        zillow_min_results_floor=10,
    )
    base.update(overrides)
    return Settings(**base)


def plan(**overrides) -> ResultBudget:
    call = dict(usage_usd=1.233, cap_usd=5.0, cycle_end=CYCLE_END, today=TODAY)
    call.update({k: v for k, v in overrides.items() if k != "settings"})
    return plan_result_budget(overrides.get("settings", settings()), **call)


def test_spendable_is_spread_over_the_days_left_in_the_apify_cycle():
    """$5.00 * 0.85 - $1.233 = $3.017 over 23 days = $0.131/day = 57 results."""
    budget = plan()
    assert budget.days_left == 23
    assert budget.allowed == 57
    assert budget.metered and not budget.blocked


def test_the_cycle_is_not_a_calendar_month():
    """This account bills the 21st to the 20th. Spreading the remainder over
    'days left this month' would overspend early and starve the tail."""
    assert plan(cycle_end="2026-09-20T23:59:59.999Z").days_left == 23
    assert plan(cycle_end="2026-08-31T23:59:59.999Z").days_left == 3


def test_the_configured_limit_still_caps_a_flush_budget():
    budget = plan(usage_usd=0.0, cap_usd=100.0)
    assert budget.allowed == 60, "must never exceed zillow_results_limit"
    assert "configured limit" in budget.reason


def test_the_reserve_share_is_held_back_from_the_plan_cap():
    """Reaching the real cap returns 403 on EVERY run and stops the product, so
    the gate stops at the target share and leaves the remainder untouched."""
    budget = plan(usage_usd=4.30, cap_usd=5.0)  # past 0.85 * 5.00
    assert budget.allowed == 0 and budget.blocked
    assert "target share" in budget.reason


def test_a_trickle_below_the_floor_buys_nothing_at_all():
    """A handful of rows is not worth a run; stopping leaves the reserve whole."""
    budget = plan(usage_usd=4.24, cap_usd=5.0)  # ~$0.01 spendable over 22 days
    assert budget.allowed == 0 and budget.blocked
    assert "floor" in budget.reason


def test_unreadable_usage_falls_back_to_the_configured_limit():
    """An Apify outage must not take the daily scrape down. The configured
    limit is itself bounded and chosen to fit a cycle, so degrading to it is
    safe; `metered=False` is what tells callers the number is unverified."""
    budget = plan(usage_usd=None, cap_usd=None)
    assert budget.allowed == 60 and not budget.blocked
    assert budget.metered is False


def test_read_result_budget_survives_a_failing_usage_endpoint():
    class Failing:
        def account_limits(self):
            raise SourceUnavailableError("Apify unreachable: timed out")

    budget = read_result_budget(
        settings(), token="SECRET", today=TODAY, client=Failing()
    )
    assert budget.allowed == 60 and budget.metered is False
    assert "could not read Apify usage" in budget.reason


def test_read_result_budget_uses_live_usage_when_it_is_available():
    class Live:
        def account_limits(self):
            return {
                "monthly_usage_usd": 1.233,
                "monthly_cap_usd": 5.0,
                "cycle_start": "2026-08-21T00:00:00.000Z",
                "cycle_end": CYCLE_END,
            }

    budget = read_result_budget(settings(), token="SECRET", today=TODAY, client=Live())
    assert budget.allowed == 57 and budget.metered


def test_a_zero_result_limit_is_refused_before_any_arithmetic():
    assert plan(settings=settings(zillow_results_limit=0)).allowed == 0


# --- the gate's effect on adapter construction ------------------------------


def _env(tmp_path):
    path = tmp_path / ".env"
    path.write_text("APIFY_TOKEN=SECRET\n", encoding="utf-8")
    return str(path)


def test_a_blocked_budget_builds_no_adapter_and_says_why(tmp_path):
    adapters, warnings = configured_adapters(
        settings(), env_path=_env(tmp_path), results_limit_override=0
    )
    assert adapters == []
    assert any("result budget allows no paid results" in w for w in warnings)


def test_the_budget_lowers_the_results_limit_and_the_per_run_charge_cap(tmp_path):
    (adapter,), warnings = configured_adapters(
        settings(zillow_max_charge_usd=0.25),
        env_path=_env(tmp_path),
        results_limit_override=20,
    )
    assert warnings == []
    assert adapter.results_limit == 20, "the gate's answer must bound the run"
    assert adapter.max_charge_usd == pytest.approx(20 * 0.0023 * 1.5)


def test_an_absent_override_leaves_the_configured_limit_untouched(tmp_path):
    """Non-scrape callers (TUI status, diagnostics) must not need a network
    call to describe a source."""
    (adapter,), _ = configured_adapters(settings(), env_path=_env(tmp_path))
    assert adapter.results_limit == 60


# --- days-on-Zillow ---------------------------------------------------------


def _filters(url: str) -> dict:
    encoded = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return json.loads(encoded["searchQueryState"][0])["filterState"]


def test_days_on_zillow_bounds_the_window_by_recency():
    filters = _filters(rental_search_url(SourceQuery("Brooklyn", "NY"), days_on_zillow=7))
    assert filters["doz"] == {"value": 7}, (
        "the actor rejects the whole URL when doz carries a STRING value; "
        "verified against the live actor on 2026-08-29"
    )


def test_days_on_zillow_is_absent_unless_configured():
    assert "doz" not in _filters(rental_search_url(SourceQuery("Brooklyn", "NY")))


@pytest.mark.parametrize("bad", [2, 5, 60, 0, -7])
def test_an_unsupported_day_window_is_refused_at_config_time(bad):
    """Zillow does not reject an unsupported doz value — it silently drops the
    filter, widening the window and the per-result bill without saying so."""
    with pytest.raises(ValueError, match="zillow_days_on_zillow"):
        settings(zillow_days_on_zillow=bad)


@pytest.mark.parametrize("good", [1, 7, 14, 30, 90])
def test_every_window_zillow_supports_is_accepted(good):
    assert settings(zillow_days_on_zillow=good).zillow_days_on_zillow == good


def test_the_configured_window_reaches_the_actor(tmp_path):
    (adapter,), _ = configured_adapters(
        settings(zillow_days_on_zillow=7), env_path=_env(tmp_path)
    )
    payload = adapter.actor_input(
        SourceQuery("Brooklyn", "NY", price_min=5000, price_max=10000)
    )
    assert _filters(payload["searchUrls"][0]["url"])["doz"] == {"value": 7}
