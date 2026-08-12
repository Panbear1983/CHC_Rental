"""Source registry and opt-in adapter construction."""

from __future__ import annotations

from chc_rental.models import Settings
from chc_rental.sources.base import SourceAdapter
from chc_rental.sources.rentcast import RentCastAdapter, load_rentcast_key
from chc_rental.sources.zillow import ZillowRentalAdapter, load_apify_token

KNOWN_SOURCES = ("rentcast", "zillow")


def configured_adapters(
    settings: Settings, *, env_path: str = ".env"
) -> tuple[list[SourceAdapter], list[str]]:
    """Build every enabled adapter without ever returning source credentials."""
    adapters: list[SourceAdapter] = []
    warnings: list[str] = []

    rentcast_key = load_rentcast_key(env_path)
    if rentcast_key is not None and settings.source_request_budget("rentcast") > 0:
        adapters.append(RentCastAdapter(api_key=rentcast_key))
    elif rentcast_key is not None:
        warnings.append("RentCast is disabled by its zero request budget")

    if settings.zillow_enabled:
        token = load_apify_token(env_path)
        if token is None:
            warnings.append("Zillow is enabled but APIFY_TOKEN is absent or a placeholder")
        elif settings.source_request_budget("zillow") <= 0:
            warnings.append("Zillow is enabled but disabled by its zero request budget")
        elif settings.zillow_results_limit <= 0:
            warnings.append("Zillow is enabled but zillow_results_limit is zero")
        else:
            adapters.append(
                ZillowRentalAdapter(
                    token=token,
                    actor=settings.zillow_actor,
                    results_limit=settings.zillow_results_limit,
                    timeout=float(settings.zillow_timeout_seconds),
                    max_charge_usd=settings.zillow_max_charge_usd,
                )
            )

    return adapters, warnings


__all__ = ["KNOWN_SOURCES", "configured_adapters"]
