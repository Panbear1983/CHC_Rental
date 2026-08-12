"""Per-user shadow outbox: caps, overlap, quiet hours and legacy suppression."""

from __future__ import annotations

from datetime import timedelta

import pytest

from chc_rental.dedup import dedup_key
from chc_rental.event_store import AlertStoreError
from chc_rental.models import DeliveryMode

from tests.test_listing_events import NOW, cycle, prepare, raw_listing


def test_overlapping_saved_searches_are_explained_by_one_outbox_row(store):
    from tests.conftest import make_search

    settings = prepare(
        store,
        searches=[
            make_search(name="First", state="TX"),
            make_search(name="Second", state="TX"),
        ],
    )
    cycle(store, settings, [raw_listing("a")], NOW)
    _, processed = cycle(
        store,
        settings,
        [raw_listing("a"), raw_listing("b", address="200 Main St")],
        NOW + timedelta(hours=3),
    )
    assert processed.outbox.queued == 1
    row = store.event_store().outbox_records()[0]
    assert row.primary_search_name == "First"
    assert len(row.matching_search_ids) == 2
    assert "First" in row.message_text


def test_daily_cap_is_reserved_transactionally_while_planning(store):
    from tests.conftest import make_search

    settings = prepare(store, searches=[make_search(state="TX", daily_cap=1)])
    cycle(store, settings, [raw_listing("a")], NOW)
    _, processed = cycle(
        store,
        settings,
        [
            raw_listing("b", address="200 Main St"),
            raw_listing("c", address="300 Main St"),
        ],
        NOW + timedelta(hours=3),
    )
    assert processed.outbox.queued == 1
    assert processed.outbox.cap_suppressed == 1
    assert len(store.event_store().outbox_records()) == 1


def test_legacy_daily_seen_key_suppresses_incremental_shadow_alert(store):
    settings = prepare(store)
    cycle(store, settings, [raw_listing("a")], NOW)
    b = raw_listing("b", address="200 Main St")
    from chc_rental.models import Listing
    from chc_rental.sources.zillow import ZillowRentalAdapter

    listing = Listing.model_validate(ZillowRentalAdapter(token="x")._canonical(b))
    store.mark_seen(111, dedup_key(listing), search_name="Primary", url=listing.url)
    _, processed = cycle(store, settings, [b], NOW + timedelta(hours=3))
    assert processed.outbox.legacy_seen_suppressed == 1
    assert store.event_store().outbox_records() == []


def test_immediate_alert_during_cross_midnight_quiet_hours_is_deferred(store):
    settings = prepare(
        store,
        profile_overrides={
            "delivery_mode": DeliveryMode.IMMEDIATE,
            "timezone": "UTC",
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "08:00",
        },
    )
    baseline_time = NOW + timedelta(hours=5)
    cycle(store, settings, [raw_listing("a")], baseline_time)
    alert_time = baseline_time + timedelta(hours=3)
    cycle(store, settings, [raw_listing("b", address="200 Main St")], alert_time)
    row = store.event_store().outbox_records()[0]
    assert row.not_before.startswith("2026-01-16T08:00:00")


def test_only_definitely_failed_outbox_items_can_be_retried(store):
    settings = prepare(store)
    cycle(store, settings, [raw_listing("a")], NOW)
    cycle(
        store,
        settings,
        [raw_listing("b", address="200 Main St")],
        NOW + timedelta(hours=3),
    )
    event_store = store.event_store()
    row = event_store.outbox_records()[0]
    with event_store.connection() as connection:
        connection.execute(
            "UPDATE outbox SET status='failed', last_error='definite rejection' "
            "WHERE outbox_id=?",
            (row.outbox_id,),
        )
        connection.commit()
    event_store.retry_failed_outbox(
        row.outbox_id, now_utc=NOW + timedelta(hours=4)
    )
    assert event_store.outbox_records()[0].status == "retry_wait"
    with event_store.connection() as connection:
        connection.execute(
            "UPDATE outbox SET status='uncertain' WHERE outbox_id=?", (row.outbox_id,)
        )
        connection.commit()
    with pytest.raises(AlertStoreError, match="not definitely failed"):
        event_store.retry_failed_outbox(
            row.outbox_id, now_utc=NOW + timedelta(hours=5)
        )
