"""Rollout readiness is evidence-backed, explicit, and non-activating."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import chc_rental.cli as cli
from chc_rental.delivery_worker import DeliveryWorker
from chc_rental.operations import (
    ROLLOUT_ATTESTATIONS,
    incremental_rollout_readiness,
)

from tests.test_delivery_worker import RecordingSender, shadow_for_peter
from tests.test_listing_events import NOW, cycle, raw_listing
from tests.test_recovery import configured_store


def attest_all(store, gates, *, now):
    for gate in gates:
        store.event_store().attest_rollout_gate(
            gate,
            evidence=f"test evidence for {gate}",
            now_utc=now,
        )


def test_first_canary_readiness_requires_and_accepts_explicit_evidence(store):
    settings, _ = shadow_for_peter(store)
    settings = settings.model_copy(update={"incremental_monthly_budget_usd": 20.0})
    store.save_settings(settings)
    blocked = incremental_rollout_readiness(
        store,
        settings,
        now_utc=NOW + timedelta(hours=3),
        token_ready=True,
        telegram_ready=True,
    )
    assert blocked["ready_for_first_canary"] is False
    assert "attestation:message_preview_approved" in blocked["blockers"]

    attest_all(
        store,
        ROLLOUT_ATTESTATIONS[:4],
        now=NOW + timedelta(hours=3),
    )
    ready = incremental_rollout_readiness(
        store,
        settings,
        now_utc=NOW + timedelta(hours=3),
        token_ready=True,
        telegram_ready=True,
    )
    assert ready["ready_for_first_canary"] is True
    assert ready["ready_for_recipient_expansion"] is False
    assert ready["source_metrics"]["p95_cost_usd"] == pytest.approx(0.05)


def test_recipient_expansion_requires_receipt_healthy_48h_and_review(store):
    settings, row = shadow_for_peter(store)
    settings = settings.model_copy(update={"incremental_monthly_budget_usd": 20.0})
    store.save_settings(settings)
    DeliveryWorker(store, sender=RecordingSender()).run(
        now_utc=NOW + timedelta(hours=3),
        telegram_ids={111},
        outbox_id=row.outbox_id,
    )
    cycle(
        store,
        settings,
        [raw_listing("a"), raw_listing("b", address="200 Main St")],
        NOW + timedelta(hours=52),
    )
    events = store.event_store()
    for hour in range(3, 53):
        events.record_scheduler_tick(
            mode="live",
            status="ok",
            source_started=0,
            source_succeeded=0,
            source_failed=0,
            delivery_sent=0,
            delivery_failed=0,
            delivery_uncertain=0,
            warning_count=0,
            details={},
            now_utc=NOW + timedelta(hours=hour),
        )
    attest_all(store, ROLLOUT_ATTESTATIONS, now=NOW + timedelta(hours=52))

    readiness = incremental_rollout_readiness(
        store,
        settings,
        now_utc=NOW + timedelta(hours=52),
        token_ready=True,
        telegram_ready=True,
    )
    assert readiness["ready_for_first_canary"] is True
    assert readiness["ready_for_recipient_expansion"] is True
    assert readiness["live_tick_window"]["window_degraded"] == 0


def test_degraded_live_tick_blocks_recipient_expansion(store):
    settings, _ = shadow_for_peter(store)
    settings = settings.model_copy(update={"incremental_monthly_budget_usd": 20.0})
    store.save_settings(settings)
    events = store.event_store()
    events.record_scheduler_tick(
        mode="live",
        status="degraded",
        source_started=1,
        source_succeeded=0,
        source_failed=1,
        delivery_sent=0,
        delivery_failed=0,
        delivery_uncertain=0,
        warning_count=1,
        details={"source_statuses": ["auth_failed"]},
        now_utc=NOW + timedelta(hours=3),
    )
    window = events.scheduler_tick_window(
        mode="live", since_utc=NOW - timedelta(hours=45)
    )
    assert window["window_degraded"] == 1
    assert window["window_source_failures"] == 1


def test_fixture_source_run_cannot_satisfy_live_cost_or_source_metrics(store):
    settings, _ = shadow_for_peter(store)
    with store.event_store().connection() as connection:
        connection.execute("UPDATE source_runs SET execution_mode='fixture'")
        connection.commit()
    readiness = incremental_rollout_readiness(
        store,
        settings.model_copy(update={"incremental_monthly_budget_usd": 20.0}),
        now_utc=NOW + timedelta(hours=3),
        token_ready=True,
        telegram_ready=True,
    )
    assert readiness["source_metrics"]["retained_runs"] == 0
    assert readiness["cost"]["known_runs"] == 0
    assert "source_metrics_recorded" in readiness["blockers"]
    assert "measured_cost_within_budget" in readiness["blockers"]


def test_cli_readiness_is_read_only_and_attestation_is_confirmed_and_audited(
    tmp_path, capsys
):
    store = configured_store(tmp_path)
    before = store.alert_db_path.read_bytes()
    code = cli.main(
        ["--root", str(tmp_path), "alerts", "readiness", "--json", "--now", NOW.isoformat()]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready_for_first_canary"] is False
    assert store.alert_db_path.read_bytes() == before

    with pytest.raises(SystemExit):
        cli.main(
            [
                "--root",
                str(tmp_path),
                "alerts",
                "attest",
                "--gate",
                "source_terms_reviewed",
                "--evidence",
                "reviewed test source record",
                "--confirm-gate",
                "message_preview_approved",
            ]
        )
    capsys.readouterr()

    assert cli.main(
        [
            "--root",
            str(tmp_path),
            "alerts",
            "attest",
            "--gate",
            "source_terms_reviewed",
            "--evidence",
            "reviewed test source record",
            "--confirm-gate",
            "source_terms_reviewed",
            "--now",
            NOW.isoformat(),
        ]
    ) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["gate"] == "source_terms_reviewed"
    with store.event_store().connection() as connection:
        action = connection.execute(
            "SELECT action FROM operator_audit ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()[0]
    assert action == "attest_rollout_gate"
