"""How many paid results today's scrape may buy.

The Apify actor bills PER RESULT, not per run. ``store.reserve_request`` counts
runs, so it has never measured spend: through 2026-08-28 a single "request"
cost between $0.012 and $0.058 depending on how many rows came back, and at a
larger ``resultsLimit`` it costs more still. This module is the meter that
actually tracks money.

It deliberately reads the ceiling from Apify rather than from a local ledger.
A local count cannot see anything else spending the same token — and something
else was: a second project ran ``zillow-detail-scraper`` against this account
daily until 2026-08-27, quietly taking ~40% of the monthly cap. Asking the
platform what has been spent is both free and the only number that is true.

Running out is not a soft failure. Reaching the plan's monthly cap returns
HTTP 403 ``platform-feature-disabled`` on EVERY actor run and stops the whole
product — no scrape, no cache, no push — as it did on 2026-08-20. So the gate
spends against ``zillow_budget_target_share`` of the cap and leaves the rest
untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from chc_rental.models import Settings
from chc_rental.sources.apify import ApifyClient
from chc_rental.sources.base import SourceError


@dataclass(frozen=True)
class ResultBudget:
    """Today's affordable result count, and the arithmetic behind it."""

    allowed: int
    reason: str
    usage_usd: Optional[float] = None
    cap_usd: Optional[float] = None
    days_left: Optional[int] = None
    metered: bool = True

    @property
    def blocked(self) -> bool:
        return self.allowed <= 0

    def summary(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "usage_usd": self.usage_usd,
            "cap_usd": self.cap_usd,
            "days_left": self.days_left,
            "metered": self.metered,
        }


def _days_left(cycle_end: Optional[str], today: date) -> Optional[int]:
    """Whole days remaining in the Apify billing cycle, today included.

    The cycle is NOT a calendar month — this account's runs from the 21st to
    the 20th — so spreading the remaining budget over "days left in the month"
    would overspend early and starve the tail.
    """
    if not cycle_end:
        return None
    try:
        end = datetime.fromisoformat(str(cycle_end).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None
    return max(1, (end - today).days + 1)


def plan_result_budget(
    settings: Settings,
    *,
    usage_usd: Optional[float],
    cap_usd: Optional[float],
    cycle_end: Optional[str],
    today: date,
) -> ResultBudget:
    """Affordable results for today. Pure: no clock, no network.

    Falls back to the configured ``zillow_results_limit`` when the platform
    would not say what has been spent. That is deliberate: an unreadable usage
    endpoint must not take the daily scrape down, and the configured limit is
    itself a bounded number chosen to fit the cycle.
    """
    configured = settings.zillow_results_limit
    if configured <= 0:
        return ResultBudget(0, "zillow_results_limit is zero", metered=False)

    if usage_usd is None or cap_usd is None or cap_usd <= 0:
        return ResultBudget(
            configured,
            "Apify usage unavailable; falling back to the configured limit",
            usage_usd=usage_usd,
            cap_usd=cap_usd,
            metered=False,
        )

    days_left = _days_left(cycle_end, today)
    spendable = cap_usd * settings.zillow_budget_target_share - usage_usd
    if spendable <= 0:
        return ResultBudget(
            0,
            f"${usage_usd:.2f} of ${cap_usd:.2f} spent is already at the "
            f"{settings.zillow_budget_target_share:.0%} target share; buying nothing",
            usage_usd=usage_usd,
            cap_usd=cap_usd,
            days_left=days_left,
        )

    per_day = spendable / days_left if days_left else spendable
    affordable = int(per_day / settings.zillow_price_per_result_usd)

    if affordable < settings.zillow_min_results_floor:
        return ResultBudget(
            0,
            f"only {affordable} results/day affordable (${per_day:.3f}), under the "
            f"floor of {settings.zillow_min_results_floor}; buying nothing",
            usage_usd=usage_usd,
            cap_usd=cap_usd,
            days_left=days_left,
        )

    allowed = min(configured, affordable)
    capped_by = "configured limit" if allowed == configured else "remaining budget"
    return ResultBudget(
        allowed,
        f"{allowed} results allowed ({capped_by}); ${usage_usd:.2f}/${cap_usd:.2f} "
        f"spent, ${spendable:.2f} left over {days_left} day(s)",
        usage_usd=usage_usd,
        cap_usd=cap_usd,
        days_left=days_left,
    )


def read_result_budget(
    settings: Settings,
    *,
    token: str,
    today: date,
    client: Optional[ApifyClient] = None,
) -> ResultBudget:
    """``plan_result_budget`` against live Apify usage. The read is free."""
    reader = client or ApifyClient(
        token=token, timeout=float(settings.zillow_timeout_seconds)
    )
    try:
        limits = reader.account_limits()
    except SourceError as exc:
        return ResultBudget(
            settings.zillow_results_limit,
            f"could not read Apify usage ({exc}); using the configured limit",
            metered=False,
        )
    return plan_result_budget(
        settings,
        usage_usd=limits["monthly_usage_usd"],
        cap_usd=limits["monthly_cap_usd"],
        cycle_end=limits["cycle_end"],
        today=today,
    )
