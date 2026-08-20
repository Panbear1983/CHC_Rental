"""Routine delivery journal lifecycle, retention, and legacy compatibility."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from chc_rental.delivery_history import RoutineDeliveryItem
from chc_rental.models import Allowlist, Settings
from chc_rental.tui.controller import TuiController

from tests.conftest import make_person

NOW = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)


def item(key: str = "v3:ny:brooklyn::1%20main%20st:2a") -> RoutineDeliveryItem:
    return RoutineDeliveryItem(
        key=key,
        search_name="Brooklyn 3bd 2ba",
        url="https://www.zillow.com/1_zpid/",
        address="1 Main St",
        unit="2A",
        city="Brooklyn",
        price=6800,
        beds=3,
        baths=2,
        source="zillow",
    )


def accepted(store, *, telegram_id: int = 111, when: datetime = NOW):
    local_day = when.date()
    staged = store.prepare_routine_delivery(
        telegram_id,
        display_name=f"Person {telegram_id}",
        timezone_name="UTC",
        local_day=local_day,
        kind="listing",
        message_text="[Filter] exact body\nhttps://www.zillow.com/1_zpid/",
        items=[item()],
        now_utc=when,
    )
    store.transition_routine_delivery(
        telegram_id,
        local_day,
        staged.attempt_id,
        state="sending",
        now_utc=when,
    )
    return store.transition_routine_delivery(
        telegram_id,
        local_day,
        staged.attempt_id,
        state="accepted",
        now_utc=when,
        telegram_message_id="700",
        chat_id=str(telegram_id),
    )


def test_accepted_routine_entry_preserves_exact_payload_and_is_per_recipient(store):
    store.save_allowlist(Allowlist(people=[make_person(111), make_person(222)]))
    entry = accepted(store)

    days = store.routine_delivery_days(111, now_utc=NOW)

    assert len(days) == 1 and days[0].listing_count == 1
    assert days[0].entries == (entry,)
    assert entry.message_text.startswith("[Filter] exact body")
    assert entry.primary_item.price == 6800
    assert entry.primary_item.url == "https://www.zillow.com/1_zpid/"
    assert entry.telegram_message_id == "700"
    assert store.routine_delivery_days(222, now_utc=NOW) == []


def test_interrupted_sending_attempt_becomes_uncertain_and_cannot_be_reprepared(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    staged = store.prepare_routine_delivery(
        111,
        display_name="Person 111",
        timezone_name="UTC",
        local_day=NOW.date(),
        kind="listing",
        message_text="exact",
        items=[item()],
        now_utc=NOW,
    )
    store.transition_routine_delivery(
        111,
        NOW.date(),
        staged.attempt_id,
        state="sending",
        now_utc=NOW,
    )

    recovered = store.prepare_routine_delivery(
        111,
        display_name="Person 111",
        timezone_name="UTC",
        local_day=NOW.date(),
        kind="listing",
        message_text="exact",
        items=[item()],
        now_utc=NOW + timedelta(minutes=1),
    )
    again = store.prepare_routine_delivery(
        111,
        display_name="Person 111",
        timezone_name="UTC",
        local_day=NOW.date(),
        kind="listing",
        message_text="exact",
        items=[item()],
        now_utc=NOW + timedelta(minutes=2),
    )

    assert recovered.status == again.status == "uncertain"
    assert "not retried" in recovered.error
    assert store.routine_delivery_days(111, now_utc=NOW)[0].uncertain_count == 1


def test_accepted_journal_suppresses_before_seen_projection_is_repaired(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    accepted(store)
    assert store.seen_keys(111) == set()

    assert item().key in store.active_seen_keys(111, now_utc=NOW)
    assert store.reconcile_routine_seen(111, now_utc=NOW) == 1
    assert store.seen_keys(111) == {item().key}


def test_diary_retention_is_independent_from_ninety_day_suppression(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    old = NOW - timedelta(days=120)
    accepted(store, when=old)

    assert item().key not in store.active_seen_keys(111, now_utc=NOW)
    days = store.routine_delivery_days(111, now_utc=NOW)
    assert [day.local_date for day in days] == [old.date()]

    removed = store.prune(
        today=(NOW + timedelta(days=366)).date(),
        settings=Settings(delivery_history_retention_days=365),
    )
    assert removed["delivery_history"] == 1
    assert store.routine_delivery_days(
        111, now_utc=NOW + timedelta(days=366)
    ) == []


def test_legacy_import_is_idempotent_and_excludes_test_push_rows(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    records = [
        {
            "key": "v3:ny:brooklyn::10%20legacy%20st:3b",
            "search": "Brooklyn",
            "url": "https://www.zillow.com/legacy_zpid/",
            "sent_at": NOW.isoformat(),
        },
        {
            "key": "notice:2026-01-15",
            "search": "",
            "url": "",
            "sent_at": NOW.isoformat(),
        },
        {
            "key": "v3:ny:brooklyn::20%20test%20st:4c",
            "search": "Brooklyn",
            "url": "https://www.zillow.com/test_zpid/",
            "sent_at": NOW.isoformat(),
            "channel": "test",
        },
    ]
    store.seen_path(111).write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    assert store.import_legacy_routine_history(111) == 2
    assert store.import_legacy_routine_history(111) == 0
    days = store.routine_delivery_days(111, now_utc=NOW)

    assert days[0].listing_count == 1
    assert days[0].notice_count == 1
    assert all(entry.legacy for entry in days[0].entries)
    listing_entry = next(entry for entry in days[0].entries if entry.items)
    assert listing_entry.primary_item.address == "10 legacy st"
    assert listing_entry.message_text == ""


def test_definitely_failed_attempt_can_be_prepared_for_a_safe_retry(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    staged = store.prepare_routine_delivery(
        111,
        display_name="Person 111",
        timezone_name="UTC",
        local_day=NOW.date(),
        kind="notice",
        message_text="No new rentals matched your searches today.",
        items=[],
        now_utc=NOW,
    )
    store.transition_routine_delivery(
        111, NOW.date(), staged.attempt_id, state="sending", now_utc=NOW
    )
    store.transition_routine_delivery(
        111,
        NOW.date(),
        staged.attempt_id,
        state="failed",
        now_utc=NOW,
        error="definite refusal",
    )

    retry = store.prepare_routine_delivery(
        111,
        display_name="Person 111",
        timezone_name="UTC",
        local_day=NOW.date(),
        kind="notice",
        message_text="No new rentals matched your searches today.",
        items=[],
        now_utc=NOW + timedelta(hours=1),
    )

    assert retry.status == "prepared"
    assert store.routine_delivery_days(111, now_utc=NOW) == []


def test_removing_and_readding_same_id_retains_its_immutable_diary(store):
    controller = TuiController(store)
    controller.add_person(111, "Original")
    accepted(store)

    controller.remove_person(111)

    assert store.routine_journal_dir(111).exists()
    controller.add_person(111, "Re-added")
    days = controller.routine_delivery_diary(111, now_utc=NOW)
    assert days[0].listing_count == 1
    assert days[0].entries[0].display_name == "Person 111"


def test_historical_test_push_is_visible_but_never_suppresses_routine(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    staged = store.prepare_routine_delivery(
        111,
        display_name="Person 111",
        timezone_name="UTC",
        local_day=NOW.date(),
        kind="listing",
        message_text="historical test batch",
        items=[item()],
        now_utc=NOW,
        legacy=True,
        channel="test",
        discriminator="receipt-701",
    )
    store.transition_routine_delivery(
        111, NOW.date(), staged.attempt_id, state="sending", now_utc=NOW
    )
    stored = store.transition_routine_delivery(
        111,
        NOW.date(),
        staged.attempt_id,
        state="accepted",
        now_utc=NOW,
        telegram_message_id="701",
        chat_id="111",
    )

    assert stored.channel == "test"
    assert store.routine_delivery_days(111, now_utc=NOW)[0].listing_count == 1
    assert item().key not in store.routine_accepted_keys(111, now_utc=NOW)
    assert store.reconcile_routine_seen(111, now_utc=NOW) == 0


def test_historical_annotation_appends_recovered_content(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    entry = accepted(store)

    updated = store.annotate_routine_delivery(
        111,
        NOW.date(),
        entry.attempt_id,
        now_utc=NOW + timedelta(minutes=1),
        message_text="recovered exact body\nhttps://www.zillow.com/1_zpid/",
        error="recovered from legacy evidence",
    )

    assert updated.status == "accepted"
    assert updated.message_text.startswith("recovered exact body")
    assert updated.error == "recovered from legacy evidence"
    assert updated.telegram_message_id == "700"
    events = store._routine_events(111, NOW.date())
    assert events[-1]["state"] == "annotated"
