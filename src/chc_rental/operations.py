"""Shared, read-only cost and cadence calculations for CLI and dashboard."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from chc_rental.models import Settings
from chc_rental.store import Store


FIRST_CANARY_ATTESTATIONS = (
    "source_terms_reviewed",
    "message_preview_approved",
    "projected_cost_approved",
    "kill_switches_tested",
)
EXPANSION_ATTESTATIONS = (
    "canary_48h_reviewed",
    "unattended_2d_reviewed",
)
ROLLOUT_ATTESTATIONS = FIRST_CANARY_ATTESTATIONS + EXPANSION_ATTESTATIONS


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


def incremental_rollout_readiness(
    store: Store,
    settings: Settings,
    *,
    now_utc: datetime,
    token_ready: bool,
    telegram_ready: bool,
) -> dict[str, Any]:
    """Evaluate first-canary and recipient-expansion gates without mutation."""
    now = now_utc.astimezone(timezone.utc)
    config = store.config_v2_status()
    ledger = store.alert_migration_status()
    checks: list[dict[str, Any]] = []

    def add(identifier: str, phase: str, ok: bool, detail: str) -> None:
        checks.append({"id": identifier, "phase": phase, "ok": bool(ok), "detail": detail})

    add("config_migrated", "first_canary", config["ready"], str(config))
    add("ledger_migrated", "first_canary", ledger.ready, str(ledger.as_dict()))
    add(
        "collection_gates",
        "first_canary",
        settings.incremental_alerts_enabled and settings.zillow_enabled,
        (
            f"incremental={settings.incremental_alerts_enabled}, "
            f"zillow={settings.zillow_enabled}"
        ),
    )
    add(
        "live_push_gate",
        "first_canary",
        settings.live_push_enabled,
        f"live_push_enabled={settings.live_push_enabled}",
    )
    add("apify_token", "first_canary", token_ready, "present" if token_ready else "missing")
    add(
        "telegram_token",
        "first_canary",
        telegram_ready,
        "present" if telegram_ready else "missing",
    )
    hard_caps_ok = (
        settings.zillow_max_charge_usd > 0
        and settings.zillow_results_limit > 0
        and settings.source_request_budget("zillow") > 0
        and settings.global_daily_request_budget > 0
    )
    add(
        "hard_caps",
        "first_canary",
        hard_caps_ok,
        (
            f"run=${settings.zillow_max_charge_usd:.2f}, "
            f"source/day={settings.source_request_budget('zillow')}, "
            f"global/day={settings.global_daily_request_budget}, "
            f"rows={settings.zillow_results_limit}"
        ),
    )

    allowlist = store.load_allowlist()
    canaries = [allowlist.get(item) for item in settings.incremental_canary_telegram_ids]
    canary_ok = (
        len(canaries) == 1
        and canaries[0] is not None
        and canaries[0].active
        and canaries[0].profile.delivery_mode.value == "immediate"
    )
    add(
        "single_immediate_canary",
        "first_canary",
        canary_ok,
        (
            "exactly one active, allowlisted Immediate recipient required; "
            f"configured={settings.incremental_canary_telegram_ids}"
        ),
    )

    cost: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    live_ticks: dict[str, Any] | None = None
    attestations: dict[str, Any] = {}
    if ledger.ready:
        events = store.event_store()
        health = events.health_snapshot()
        cost = incremental_cost_status(store, settings, now_utc=now)
        metrics = events.source_rollout_metrics("zillow")
        live_ticks = events.scheduler_tick_window(
            mode="live", since_utc=now - timedelta(hours=48)
        )
        attestations = {
            gate: {
                "evidence": item.evidence,
                "attested_at": item.attested_at,
            }
            for gate, item in events.rollout_attestations().items()
        }
        add(
            "active_scopes",
            "first_canary",
            health["active_scopes"] > 0,
            f"active_scopes={health['active_scopes']}",
        )
        add(
            "silent_baselines",
            "first_canary",
            health["active_scopes"] > 0 and health["pending_baselines"] == 0,
            f"pending_baselines={health['pending_baselines']}",
        )
        unprocessed = len(events.unprocessed_source_runs())
        add(
            "source_recovery_clear",
            "first_canary",
            health["running_runs"] == 0 and unprocessed == 0,
            f"running={health['running_runs']}, unprocessed={unprocessed}",
        )
        open_breakers = [
            item["source"]
            for item in health.get("breakers", [])
            if item["state"] != "closed"
        ]
        add(
            "source_breaker_closed",
            "first_canary",
            not open_breakers,
            f"open={open_breakers or 'none'}",
        )
        budget = settings.incremental_monthly_budget_usd
        projection = cost["projected_monthly_cost_usd"]
        cost_ok = (
            budget is not None
            and budget > 0
            and cost["known_runs"] > 0
            and not cost["cost_unknown"]
            and projection is not None
            and projection <= budget
            and not cost["monthly_budget_reached"]
        )
        add(
            "measured_cost_within_budget",
            "first_canary",
            cost_ok,
            f"projection={projection}, budget={budget}, unknown={cost['cost_unknown']}",
        )
        metrics_ok = (
            metrics["successful_runs"] > 0
            and metrics["p95_duration_seconds"] is not None
            and metrics["p95_cost_usd"] is not None
        )
        add(
            "source_metrics_recorded",
            "first_canary",
            metrics_ok,
            (
                f"p95_duration_seconds={metrics['p95_duration_seconds']}, "
                f"p95_cost_usd={metrics['p95_cost_usd']}, "
                f"truncated_runs={metrics['truncated_runs']}"
            ),
        )
        last_success = metrics.get("last_success_at")
        age_minutes = None
        if last_success:
            parsed = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
            age_minutes = max(0, (now - parsed).total_seconds() / 60)
        add(
            "source_fresh",
            "first_canary",
            age_minutes is not None
            and age_minutes <= settings.zillow_incremental_interval_minutes * 2,
            f"last_success_age_minutes={age_minutes}",
        )
        outbox = health["outbox"]
        canary_outbox = (
            events.outbox_counts(telegram_id=canaries[0].telegram_id)
            if canary_ok and canaries[0] is not None
            else {}
        )
        staged_or_sent = sum(
            canary_outbox.get(state, 0)
            for state in ("shadow", "pending", "retry_wait", "sent")
        )
        add(
            "canary_message_staged_or_sent",
            "first_canary",
            staged_or_sent > 0,
            f"eligible_canary_rows={staged_or_sent}",
        )
        add(
            "delivery_review_clear",
            "first_canary",
            outbox.get("failed", 0) == 0 and outbox.get("uncertain", 0) == 0,
            (
                f"failed={outbox.get('failed', 0)}, "
                f"uncertain={outbox.get('uncertain', 0)}"
            ),
        )
        add(
            "canary_receipt_recorded",
            "expansion",
            outbox.get("sent", 0) > 0,
            f"sent_receipts={outbox.get('sent', 0)}",
        )

        first_tick = live_ticks.get("first_tick_at")
        last_tick = live_ticks.get("last_tick_at")
        span_hours = 0.0
        last_age_minutes = None
        if first_tick and last_tick:
            first = datetime.fromisoformat(str(first_tick).replace("Z", "+00:00"))
            last = datetime.fromisoformat(str(last_tick).replace("Z", "+00:00"))
            span_hours = max(0.0, (last - first).total_seconds() / 3600)
            last_age_minutes = max(0.0, (now - last).total_seconds() / 60)
        permitted_gap = max(60, settings.incremental_scheduler_tick_minutes * 4)
        max_gap = live_ticks.get("max_gap_minutes")
        live_window_ok = (
            span_hours >= 48
            and last_age_minutes is not None
            and last_age_minutes <= permitted_gap
            and live_ticks["window_degraded"] == 0
            and live_ticks["window_source_failures"] == 0
            and live_ticks["window_delivery_failures"] == 0
            and live_ticks["window_uncertain"] == 0
            and max_gap is not None
            and max_gap <= permitted_gap
        )
        add(
            "healthy_live_window_48h",
            "expansion",
            live_window_ok,
            (
                f"span_hours={span_hours:.1f}, window_ticks={live_ticks['window_ticks']}, "
                f"degraded={live_ticks['window_degraded']}, "
                f"max_gap_minutes={max_gap}, last_age_minutes={last_age_minutes}"
            ),
        )
    else:
        for identifier, phase in (
            ("active_scopes", "first_canary"),
            ("silent_baselines", "first_canary"),
            ("source_recovery_clear", "first_canary"),
            ("source_breaker_closed", "first_canary"),
            ("measured_cost_within_budget", "first_canary"),
            ("source_metrics_recorded", "first_canary"),
            ("source_fresh", "first_canary"),
            ("canary_message_staged_or_sent", "first_canary"),
            ("delivery_review_clear", "first_canary"),
            ("canary_receipt_recorded", "expansion"),
            ("healthy_live_window_48h", "expansion"),
        ):
            add(identifier, phase, False, "alert ledger migration required")

    for gate in FIRST_CANARY_ATTESTATIONS:
        add(
            f"attestation:{gate}",
            "first_canary",
            gate in attestations,
            attestations.get(gate, {}).get("attested_at", "not attested"),
        )
    for gate in EXPANSION_ATTESTATIONS:
        add(
            f"attestation:{gate}",
            "expansion",
            gate in attestations,
            attestations.get(gate, {}).get("attested_at", "not attested"),
        )

    first_checks = [item for item in checks if item["phase"] == "first_canary"]
    expansion_checks = checks
    return {
        "ready_for_first_canary": all(item["ok"] for item in first_checks),
        "ready_for_recipient_expansion": all(item["ok"] for item in expansion_checks),
        "checks": checks,
        "blockers": [item["id"] for item in checks if not item["ok"]],
        "source_metrics": metrics,
        "cost": cost,
        "live_tick_window": live_ticks,
        "attestations": attestations,
        "allowed_attestation_gates": list(ROLLOUT_ATTESTATIONS),
    }
