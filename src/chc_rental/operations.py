"""Shared, read-only cost and cadence calculations for CLI and dashboard."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from chc_rental.models import Settings
from chc_rental.store import Store


def incremental_cost_status(
    store: Store, settings: Settings, *, now_utc: datetime
) -> dict[str, Any]:
    monthly = store.event_store().monthly_source_cost("zillow", now_utc=now_utc)
    health = store.event_store().health_snapshot()
    active_scopes = int(health["active_scopes"])
    start_hour, start_minute = map(int, settings.incremental_active_start.split(":"))
    end_hour, end_minute = map(int, settings.incremental_active_end.split(":"))
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    active_minutes = end - start if end > start else (24 * 60 - start + end)
    runs_per_scope_day = max(
        1, math.ceil(active_minutes / settings.zillow_incremental_interval_minutes)
    )
    uncapped_daily_runs = active_scopes * runs_per_scope_day
    capped_daily_runs = min(
        uncapped_daily_runs,
        settings.source_request_budget("zillow"),
        settings.global_daily_request_budget,
    )
    projected = (
        capped_daily_runs * monthly["p95_cost_usd"] * 30
        if monthly["p95_cost_usd"] is not None
        else None
    )
    ceiling = settings.incremental_monthly_budget_usd
    return {
        **monthly,
        "active_scopes": active_scopes,
        "runs_per_scope_day": runs_per_scope_day,
        "uncapped_daily_runs": uncapped_daily_runs,
        "capped_daily_runs": capped_daily_runs,
        "projected_monthly_cost_usd": projected,
        "monthly_budget_usd": ceiling,
        "monthly_budget_reached": (
            ceiling is not None and monthly["known_cost_usd"] >= ceiling
        ),
        "cost_unknown": monthly["unknown_runs"] > 0,
        "automatic_cadence_increase_allowed": (
            ceiling is not None
            and monthly["unknown_runs"] == 0
            and monthly["p95_cost_usd"] is not None
            and projected is not None
            and monthly["known_cost_usd"] < ceiling
            and projected <= ceiling
        ),
    }
