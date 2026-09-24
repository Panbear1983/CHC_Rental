"""Source registry and adapter construction.

Zillow, collected through the managed Apify actor, is the ONLY listing source.
RentCast was removed on 2026-08-13: its records carried no consumer listing link
(only a Google-Maps-of-the-address fallback), which delivered no value to
recipients. If a second licensed source is ever added, give it its own adapter
and add it back to KNOWN_SOURCES here.
"""

from __future__ import annotations

from chc_rental.models import Settings
from chc_rental.sources.base import SourceAdapter
from chc_rental.sources.zillow import ZillowRentalAdapter, load_apify_token

KNOWN_SOURCES = ("zillow",)


def enabled_cache_sources(settings: Settings) -> list[str]:
    """Sources whose saved daily snapshots delivery is allowed to consume.

    This intentionally ignores credentials: sending from an already-saved
    snapshot must not require APIFY_TOKEN and must never construct an adapter.
    """
    return ["zillow"] if settings.zillow_enabled else []


def configured_adapters(
    settings: Settings,
    *,
    env_path: str = ".env",
    results_limit_override: int | None = None,
) -> tuple[list[SourceAdapter], list[str]]:
    """Build every enabled adapter without ever returning source credentials.

    A returned warning marks a source that was SUPPOSED to run but could not
    (missing token, zero budget). Those warnings make the fetch pool
    "incomplete", which suppresses dishonest "nothing matched" notices. A
    healthy enabled Zillow yields no warnings, so no-results notices flow.

    ``results_limit_override`` is the spend gate's answer for today (see
    ``chc_rental.cost``). Because the actor bills per RESULT, this — not the
    request ledger — is what bounds the bill. None keeps the configured limit,
    which is what every non-scrape caller (TUI status, diagnostics) wants,
    since none of them should make a network call to price a run.
    """
    adapters: list[SourceAdapter] = []
    warnings: list[str] = []

    # A deliberately-disabled source is an intentional operator state (a kill
    # switch), NOT a coverage gap: it stays silent so it never marks the fetch
    # pool "incomplete" and suppresses honest no-results notices. Only an
    # ENABLED-but-broken source warns. (An empty adapter list still stops the
    # run; the CLI reports "no listing source configured" from that.)
    if not settings.zillow_enabled:
        return adapters, warnings

    token = load_apify_token(env_path)
    if token is None:
        warnings.append("Zillow is enabled but APIFY_TOKEN is absent or a placeholder")
    elif settings.source_request_budget("zillow") <= 0:
        warnings.append("Zillow is enabled but disabled by its zero request budget")
    elif settings.zillow_results_limit <= 0:
        warnings.append("Zillow is enabled but zillow_results_limit is zero")
    elif results_limit_override is not None and results_limit_override <= 0:
        warnings.append(
            "Zillow is enabled but today's result budget allows no paid results"
        )
    else:
        limit = (
            settings.zillow_results_limit
            if results_limit_override is None
            else min(settings.zillow_results_limit, results_limit_override)
        )
        adapters.append(
            ZillowRentalAdapter(
                token=token,
                actor=settings.zillow_actor,
                results_limit=limit,
                timeout=float(settings.zillow_timeout_seconds),
                # Per-run belt to the budget's braces: even a runaway actor
                # cannot bill past what this run was authorised to buy.
                max_charge_usd=min(
                    settings.zillow_max_charge_usd,
                    limit * settings.zillow_price_per_result_usd * 1.5,
                ),
                days_on_zillow=settings.zillow_days_on_zillow,
            )
        )

    return adapters, warnings


__all__ = ["KNOWN_SOURCES", "configured_adapters", "enabled_cache_sources"]
