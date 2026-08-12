"""Canary delivery state machine; all transports are local fakes."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import chc_rental.cli as cli
from chc_rental.delivery_worker import DeliveryWorker
from chc_rental.models import Allowlist, DeliveryMode, Profile
from chc_rental.notify.telegram import TelegramReceipt, TelegramSendError

from tests.conftest import make_person, make_search
from tests.test_listing_events import NOW, cycle, prepare, raw_listing


class RecordingSender:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.sent = []

    def send(self, *, telegram_id, text):
        self.sent.append((telegram_id, text))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
        return TelegramReceipt(message_id=str(9000 + len(self.sent)), chat_id=str(telegram_id))


def shadow_for_peter(store, *, save_settings=True):
    settings = prepare(
        store,
        profile_overrides={
            "delivery_mode": DeliveryMode.IMMEDIATE,
            "timezone": "UTC",
        },
    ).model_copy(
        update={
            "live_push_enabled": True,
            "incremental_alerts_enabled": True,
            "zillow_enabled": True,
            "incremental_canary_telegram_ids": [111],
        }
    )
    if save_settings:
        store.save_settings(settings)
    cycle(store, settings, [raw_listing("a")], NOW)
    cycle(
        store,
        settings,
        [raw_listing("b", address="200 Main St")],
        NOW + timedelta(hours=3),
    )
    row = store.event_store().outbox_records()[0]
    assert row.status == "shadow"
    return settings, row


def test_success_records_receipt_marks_daily_seen_and_never_replays(store):
    _, row = shadow_for_peter(store)
    sender = RecordingSender()
    report = DeliveryWorker(store, sender=sender).run(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}, outbox_id=row.outbox_id
    )
    assert report.promoted == 1 and report.sent == 1
    assert sender.sent[0][0] == 111
    assert "Newly observed rental (first seen by CHC)" in sender.sent[0][1]
    sent = store.event_store().outbox_records()[0]
    assert sent.status == "sent" and sent.identity_key in store.seen_keys(111)
    with store.event_store().connection() as connection:
        receipt = connection.execute(
            "SELECT telegram_message_id FROM delivery_receipts WHERE outbox_id=?",
            (row.outbox_id,),
        ).fetchone()
    assert receipt[0] == "9001"

    replay = DeliveryWorker(store, sender=sender).run(
        now_utc=NOW + timedelta(hours=4), telegram_ids={111}
    )
    assert replay.sent == 0 and len(sender.sent) == 1


@pytest.mark.parametrize(
    "disabled_field",
    ["live_push_enabled", "incremental_alerts_enabled", "zillow_enabled"],
)
def test_each_global_gate_independently_blocks_promotion(store, disabled_field):
    settings, row = shadow_for_peter(store)
    store.save_settings(settings.model_copy(update={disabled_field: False}))
    report = DeliveryWorker(store, sender=RecordingSender()).run(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}
    )
    assert report.sent == 0 and report.blocked
    assert store.event_store().outbox_records()[0].status == "shadow"


def test_daily_delivery_mode_blocks_incremental_canary_without_cancelling(store):
    _, row = shadow_for_peter(store)
    with store.edit_allowlist() as allowlist:
        allowlist.people[0].profile.delivery_mode = DeliveryMode.DAILY
    report = DeliveryWorker(store, sender=RecordingSender()).run(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}
    )
    assert report.sent == 0 and "not immediate" in " ".join(report.blocked)
    assert store.event_store().outbox_records()[0].status == "shadow"


def test_only_configured_canary_is_promoted_when_two_people_match(store):
    profile = lambda: Profile(
        delivery_mode=DeliveryMode.IMMEDIATE,
        timezone="UTC",
        searches=[make_search(name="Primary", state="TX")],
    )
    store.save_allowlist(
        Allowlist(
            people=[
                make_person(111, profile=profile()),
                make_person(222, profile=profile()),
            ]
        )
    )
    store.migrate_config_v2()
    store.migrate_alert_ledger()
    settings = store.load_settings().model_copy(
        update={
            "live_push_enabled": True,
            "incremental_alerts_enabled": True,
            "zillow_enabled": True,
            "incremental_canary_telegram_ids": [111],
            "incremental_max_new_starts_per_cycle": 10,
            "source_daily_request_budgets": {"zillow": 20},
        }
    )
    store.save_settings(settings)
    cycle(store, settings, [raw_listing("a")], NOW)
    cycle(
        store,
        settings,
        [raw_listing("b", address="200 Main St")],
        NOW + timedelta(hours=3),
    )
    assert len(store.event_store().outbox_records(status="shadow")) == 2
    sender = RecordingSender()
    report = DeliveryWorker(store, sender=sender).run(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}
    )
    assert report.sent == 1 and sender.sent[0][0] == 111
    states = {row.telegram_id: row.status for row in store.event_store().outbox_records()}
    assert states == {111: "sent", 222: "shadow"}


def test_removal_after_promotion_is_rechecked_and_cancelled(store):
    _, row = shadow_for_peter(store)
    worker = DeliveryWorker(store, sender=RecordingSender())
    assert worker.promote(now_utc=NOW + timedelta(hours=3), telegram_ids={111}).promoted == 1
    with store.edit_allowlist() as allowlist:
        allowlist.people[0].active = False
    report = worker.deliver_due(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}
    )
    assert report.cancelled == 1 and report.sent == 0
    assert store.event_store().outbox_records()[0].status == "cancelled"


def test_changed_search_is_rechecked_before_transport(store):
    _, row = shadow_for_peter(store)
    worker = DeliveryWorker(store, sender=RecordingSender())
    worker.promote(now_utc=NOW + timedelta(hours=3), telegram_ids={111})
    with store.edit_allowlist() as allowlist:
        allowlist.people[0].profile.searches[0].price_max = 1000
    report = worker.deliver_due(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}
    )
    assert report.cancelled == 1 and report.sent == 0


def test_new_quiet_hours_are_rechecked_immediately_before_transport(store):
    _, row = shadow_for_peter(store)
    worker = DeliveryWorker(store, sender=RecordingSender())
    worker.promote(now_utc=NOW + timedelta(hours=3), telegram_ids={111})
    with store.edit_allowlist() as allowlist:
        profile = allowlist.people[0].profile
        profile.quiet_hours_start = "17:00"
        profile.quiet_hours_end = "20:00"
    report = worker.deliver_due(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}
    )
    assert report.sent == 0 and "quiet hours" in " ".join(report.blocked)
    pending = store.event_store().outbox_records()[0]
    assert pending.status == "pending"
    assert pending.not_before.startswith("2026-01-15T20:00:00")


def test_definite_transient_failure_uses_bounded_retry_then_succeeds(store):
    _, row = shadow_for_peter(store)
    sender = RecordingSender(
        [TelegramSendError("sendMessage refused: retry later", retry_after=60)]
    )
    worker = DeliveryWorker(store, sender=sender)
    first = worker.run(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}, outbox_id=row.outbox_id
    )
    assert first.retry_wait == 1
    assert store.event_store().outbox_records()[0].status == "retry_wait"
    too_soon = worker.deliver_due(
        now_utc=NOW + timedelta(hours=3, seconds=59), telegram_ids={111}
    )
    assert too_soon.sent == 0 and len(sender.sent) == 1
    retried = worker.deliver_due(
        now_utc=NOW + timedelta(hours=3, seconds=60), telegram_ids={111}
    )
    assert retried.sent == 1 and len(sender.sent) == 2
    assert store.event_store().outbox_records()[0].attempts == 2


def test_ambiguous_transport_failure_is_quarantined_without_retry(store):
    _, row = shadow_for_peter(store)
    sender = RecordingSender(
        [TelegramSendError("socket reset", ambiguous=True)]
    )
    worker = DeliveryWorker(store, sender=sender)
    first = worker.run(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}, outbox_id=row.outbox_id
    )
    assert first.uncertain == 1
    assert store.event_store().outbox_records()[0].status == "uncertain"
    worker.deliver_due(now_utc=NOW + timedelta(days=1), telegram_ids={111})
    assert len(sender.sent) == 1


def test_stale_sending_after_restart_becomes_uncertain_not_resent(store):
    _, row = shadow_for_peter(store)
    sender = RecordingSender()
    worker = DeliveryWorker(store, sender=sender, sending_stale_minutes=10)
    worker.promote(now_utc=NOW + timedelta(hours=3), telegram_ids={111})
    claimed = store.event_store().claim_due_outbox(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}
    )
    assert claimed is not None and claimed.status == "sending"
    report = worker.deliver_due(
        now_utc=NOW + timedelta(hours=3, minutes=11), telegram_ids={111}
    )
    assert report.stale_sending_recovered == 1 and report.uncertain == 1
    assert sender.sent == []
    assert store.event_store().outbox_records()[0].status == "uncertain"


def test_terminal_telegram_rejection_stays_failed_for_manual_review(store):
    _, row = shadow_for_peter(store)
    sender = RecordingSender(
        [TelegramSendError("bot was blocked", terminal=True)]
    )
    report = DeliveryWorker(store, sender=sender).run(
        now_utc=NOW + timedelta(hours=3), telegram_ids={111}, outbox_id=row.outbox_id
    )
    assert report.failed == 1
    assert store.event_store().outbox_records()[0].status == "failed"
    assert [item.outbox_id for item in store.event_store().terminal_failures_needing_alert()] == [
        row.outbox_id
    ]


def test_requesting_non_canary_recipient_is_refused_before_send(store):
    shadow_for_peter(store)
    with pytest.raises(ValueError, match="not configured canaries"):
        DeliveryWorker(store, sender=RecordingSender()).run(
            now_utc=NOW + timedelta(hours=3), telegram_ids={999}
        )


def test_cli_delivery_defaults_to_read_only_dry_run(store, capsys, monkeypatch):
    _, row = shadow_for_peter(store)
    monkeypatch.setattr(
        cli, "build_sender", lambda path: (_ for _ in ()).throw(AssertionError("no sender"))
    )
    assert cli.main(["--root", str(store.root), "alerts", "deliver"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert "no Telegram call" in payload["note"]
    assert store.event_store().outbox_records()[0].status == "shadow"


def test_cli_live_delivery_requires_explicit_canary_and_can_target_one_row(
    store, capsys, monkeypatch
):
    _, row = shadow_for_peter(store)
    sender = RecordingSender()
    monkeypatch.setattr(cli, "build_sender", lambda path: sender)
    with pytest.raises(SystemExit):
        cli.main(["--root", str(store.root), "alerts", "deliver", "--live"])
    assert sender.sent == []

    code = cli.main(
        [
            "--root",
            str(store.root),
            "alerts",
            "deliver",
            "--live",
            "--confirm-telegram-id",
            "111",
            "--outbox-id",
            str(row.outbox_id),
            "--now",
            (NOW + timedelta(hours=3)).isoformat(),
        ]
    )
    assert code == 0 and len(sender.sent) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["sent_outbox_ids"] == [row.outbox_id]
