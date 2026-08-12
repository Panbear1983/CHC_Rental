"""First-observation baseline and material listing-event classification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from chc_rental.events import observe_collection
from chc_rental.event_store import AlertStoreError
from chc_rental.incremental import IncrementalCollector
from chc_rental.models import Allowlist, Profile
from chc_rental.outbox import process_incremental_report
from chc_rental.sources.apify import ApifyRunState
from chc_rental.sources.zillow import ZillowRentalAdapter

from tests.conftest import make_person, make_search

NOW = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
BOUNDS = {"west": -98.0, "east": -97.0, "south": 30.0, "north": 31.0}


def raw_listing(zpid: str, *, address="100 Main St", price=2200):
    return {
        "zpid": zpid,
        "detailUrl": f"/homedetails/{zpid}_zpid/",
        "statusType": "FOR_RENT",
        "addressStreet": address,
        "addressCity": "Austin",
        "addressState": "TX",
        "addressZipcode": "78701",
        "unformattedPrice": price,
        "beds": 2,
        "baths": 1.5,
        "area": 900,
        "homeType": "APARTMENT",
    }


class ImmediateClient:
    def __init__(self, raw):
        self.raw = list(raw)

    def start_actor(self, actor, payload, *, max_total_charge_usd):
        run_id = f"remote-{uuid4()}"
        return ApifyRunState(run_id, "SUCCEEDED", run_id, 0.05, "done")

    def get_dataset(self, dataset_id):
        return self.raw

    def get_run(self, run_id):  # pragma: no cover - starts finish immediately
        return ApifyRunState(run_id, "SUCCEEDED", run_id, 0.05, "done")


def prepare(store, *, searches=None, profile_overrides=None):
    searches = searches or [make_search(name="Primary", state="TX", daily_cap=5)]
    profile = Profile(searches=searches, **(profile_overrides or {}))
    store.save_allowlist(
        Allowlist(schema_version=1, people=[make_person(111, profile=profile)])
    )
    store.migrate_config_v2()
    store.migrate_alert_ledger()
    return store.load_settings().model_copy(
        update={
            "incremental_alerts_enabled": True,
            "zillow_enabled": True,
            "incremental_max_new_starts_per_cycle": 10,
            "source_daily_request_budgets": {"zillow": 50},
        }
    )


def cycle(store, settings, raw, now):
    collected = IncrementalCollector(
        store,
        settings=settings,
        adapter=ZillowRentalAdapter(
            token="SECRET",
            results_limit=25,
            bounds_resolver=lambda query: BOUNDS,
        ),
        client=ImmediateClient(raw),
        worker_id="event-test",
    ).cycle(now_utc=now)
    return collected, process_incremental_report(store, collected, now_utc=now)


def test_first_usable_window_is_silent_then_one_new_listing_queues_once(store):
    settings = prepare(store)
    a = raw_listing("a")
    first, processed_first = cycle(store, settings, [a], NOW)
    assert first.records
    assert processed_first.observations[0].baseline_established is True
    assert [event.event_type for event in processed_first.observations[0].events] == [
        "baseline"
    ]
    assert store.event_store().outbox_records() == []

    b = raw_listing("b", address="200 Main St")
    _, processed_second = cycle(store, settings, [a, b], NOW + timedelta(hours=3))
    types = [event.event_type for event in processed_second.observations[0].events]
    assert types == ["duplicate", "new"]
    assert processed_second.outbox.queued == 1
    assert len(store.event_store().outbox_records(status="shadow")) == 1

    _, processed_third = cycle(store, settings, [a, b], NOW + timedelta(hours=6))
    assert processed_third.outbox.queued == 0
    assert len(store.event_store().outbox_records()) == 1


def test_empty_successful_first_run_still_establishes_baseline(store):
    settings = prepare(store)
    _, processed = cycle(store, settings, [], NOW)
    assert processed.observations[0].baseline_established is True
    assert processed.observations[0].events == []
    assert set(store.event_store().query_baseline_states().values()) == {"established"}


def test_price_change_is_recorded_but_not_queued_in_mvp(store):
    settings = prepare(store)
    cycle(store, settings, [raw_listing("a")], NOW)
    _, processed = cycle(
        store, settings, [raw_listing("a", price=2100)], NOW + timedelta(hours=3)
    )
    assert [event.event_type for event in processed.observations[0].events] == [
        "price_change"
    ]
    assert processed.outbox.queued == 0


def test_material_query_change_gets_its_own_silent_baseline(store):
    settings = prepare(store)
    listing = raw_listing("a")
    cycle(store, settings, [listing], NOW)
    with store.edit_allowlist() as allowlist:
        allowlist.people[0].profile.searches[0].price_max = 2800
    _, processed = cycle(store, settings, [listing], NOW + timedelta(hours=1))
    assert processed.observations[0].baseline_established is True
    assert [event.event_type for event in processed.observations[0].events] == [
        "baseline"
    ]
    assert store.event_store().outbox_records() == []


def test_same_person_matching_two_query_scopes_still_gets_one_outbox_item(store):
    settings = prepare(
        store,
        searches=[
            make_search(name="A", state="TX", price_min=1000),
            make_search(name="B", state="TX", price_min=1100),
        ],
    )
    a = raw_listing("a")
    cycle(store, settings, [a], NOW)
    b = raw_listing("b", address="200 Main St")
    _, processed = cycle(store, settings, [a, b], NOW + timedelta(hours=3))
    assert processed.outbox.queued == 1
    assert processed.outbox.duplicates == 1
    assert len(store.event_store().outbox_records()) == 1


def test_version_change_is_classified_independently_for_each_query_scope(store):
    settings = prepare(
        store,
        searches=[
            make_search(name="A", state="TX", price_min=1000),
            make_search(name="B", state="TX", price_min=1100),
        ],
    )
    cycle(store, settings, [raw_listing("a", price=2200)], NOW)
    _, processed = cycle(
        store,
        settings,
        [raw_listing("a", price=2100)],
        NOW + timedelta(hours=3),
    )
    assert [
        report.events[0].event_type for report in processed.observations
    ] == ["price_change", "price_change"]


def test_completed_dataset_is_recovered_if_process_stops_before_observation(store):
    settings = prepare(store)
    first_collector = IncrementalCollector(
        store,
        settings=settings,
        adapter=ZillowRentalAdapter(
            token="SECRET", results_limit=25, bounds_resolver=lambda query: BOUNDS
        ),
        client=ImmediateClient([raw_listing("a")]),
        worker_id="first-process",
    )
    first = first_collector.cycle(now_utc=NOW)
    assert first.collections[0].status == "succeeded"
    assert store.event_store().unprocessed_source_runs()

    recovered_collector = IncrementalCollector(
        store,
        settings=settings,
        adapter=ZillowRentalAdapter(
            token="SECRET", results_limit=25, bounds_resolver=lambda query: BOUNDS
        ),
        client=ImmediateClient([raw_listing("a")]),
        worker_id="restarted-process",
    )
    recovered = recovered_collector.cycle(now_utc=NOW + timedelta(hours=3))
    assert recovered.recovered == 1 and recovered.started == 0
    processed = process_incremental_report(
        store, recovered, now_utc=NOW + timedelta(hours=3)
    )
    assert processed.observations[0].baseline_established is True
    assert store.event_store().unprocessed_source_runs() == []


def test_persisted_new_event_survives_crash_before_outbox_insert(store):
    settings = prepare(store)
    cycle(store, settings, [raw_listing("a")], NOW)
    b = raw_listing("b", address="200 Main St")
    collected = IncrementalCollector(
        store,
        settings=settings,
        adapter=ZillowRentalAdapter(
            token="SECRET", results_limit=25, bounds_resolver=lambda query: BOUNDS
        ),
        client=ImmediateClient([b]),
        worker_id="crashing-process",
    ).cycle(now_utc=NOW + timedelta(hours=3))
    observed = observe_collection(
        store, collected.collections[0], now_utc=NOW + timedelta(hours=3)
    )
    assert observed.events[0].event_type == "new"
    assert store.event_store().outbox_records() == []

    recovered = IncrementalCollector(
        store,
        settings=settings,
        adapter=ZillowRentalAdapter(
            token="SECRET", results_limit=25, bounds_resolver=lambda query: BOUNDS
        ),
        client=ImmediateClient([b]),
        worker_id="recovery-process",
    ).cycle(now_utc=NOW + timedelta(hours=6))
    processed = process_incremental_report(
        store, recovered, now_utc=NOW + timedelta(hours=6)
    )
    assert processed.outbox.queued == 1
    assert store.event_store().outbox_records()[0].status == "shadow"


def test_confirmed_baseline_reset_is_audited_and_next_window_is_silent(store):
    settings = prepare(store)
    cycle(store, settings, [raw_listing("a")], NOW)
    query_id = next(iter(store.event_store().query_baseline_states()))
    store.event_store().reset_query_baseline(
        query_id,
        now_utc=NOW + timedelta(hours=1),
        reason="test owner confirmation",
    )
    assert store.event_store().query_baseline_states()[query_id] == "reset_pending"
    _, processed = cycle(
        store,
        settings,
        [raw_listing("a"), raw_listing("b", address="200 Main St")],
        NOW + timedelta(hours=3),
    )
    assert {
        event.event_type for event in processed.observations[0].events
    } == {"baseline"}
    assert processed.outbox.queued == 0
    with store.event_store().connection() as connection:
        audit = connection.execute(
            "SELECT action, target_id, details_json FROM operator_audit"
        ).fetchone()
    assert audit[0] == "reset_baseline" and audit[1] == query_id
    assert "test owner confirmation" in audit[2]


def test_baseline_reset_refuses_an_open_or_unprocessed_source_run(store):
    settings = prepare(store)
    collected = IncrementalCollector(
        store,
        settings=settings,
        adapter=ZillowRentalAdapter(
            token="SECRET", results_limit=25, bounds_resolver=lambda query: BOUNDS
        ),
        client=ImmediateClient([raw_listing("a")]),
        worker_id="unprocessed-run-test",
    ).cycle(now_utc=NOW)
    query_id = collected.collections[0].query_id
    with pytest.raises(AlertStoreError, match="open or unprocessed"):
        store.event_store().reset_query_baseline(
            query_id, now_utc=NOW + timedelta(minutes=1), reason="unsafe timing"
        )
    assert store.event_store().query_baseline_states()[query_id] == "pending"
