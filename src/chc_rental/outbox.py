"""Preference matching and durable shadow-notification planning."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from chc_rental.dedup import upgrade_seen_key
from chc_rental.events import ListingEvent, ObservationReport, observe_collection
from chc_rental.incremental import IncrementalCycleReport
from chc_rental.matching import matches_search
from chc_rental.notification_schedule import local_day_window_utc, notification_not_before
from chc_rental.pipeline import PlannedPush
from chc_rental.sources.planner import plan_incremental_queries
from chc_rental.store import Store


@dataclass
class ShadowOutboxReport:
    queued: int = 0
    duplicates: int = 0
    cap_suppressed: int = 0
    legacy_seen_suppressed: int = 0
    unmatched: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "queued": self.queued,
            "duplicates": self.duplicates,
            "cap_suppressed": self.cap_suppressed,
            "legacy_seen_suppressed": self.legacy_seen_suppressed,
            "unmatched": self.unmatched,
        }


@dataclass
class IncrementalProcessingReport:
    observations: list[ObservationReport] = field(default_factory=list)
    outbox: ShadowOutboxReport = field(default_factory=ShadowOutboxReport)

    def summary(self) -> dict[str, Any]:
        return {
            "observations": [item.summary() for item in self.observations],
            "outbox": self.outbox.summary(),
        }


def _idempotency_key(event: ListingEvent, telegram_id: int) -> str:
    # Search ID is retained on the row for cap attribution and explanation, but
    # deliberately excluded here: one property event may match several saved
    # searches and still must produce one message per person.
    material = "|".join(
        (
            "alert-v1",
            str(telegram_id),
            event.identity_key,
            event.event_type,
            event.version_hash,
        )
    )
    return "alert:" + hashlib.sha256(material.encode()).hexdigest()


def plan_shadow_notifications(
    store: Store,
    *,
    events: list[ListingEvent],
    query_id: str,
    now_utc: datetime,
) -> ShadowOutboxReport:
    report = ShadowOutboxReport()
    allowlist = store.load_allowlist()
    planned, _ = plan_incremental_queries(allowlist, source="zillow")
    scope = next((item for item in planned if item.query_id == query_id), None)
    if scope is None:
        report.unmatched += sum(event.event_type == "new" for event in events)
        return report
    watches_by_person: dict[int, set[str]] = {}
    for watch in scope.watches:
        watches_by_person.setdefault(watch.telegram_id, set()).add(watch.search_id)

    for event in events:
        if event.event_type != "new":
            continue
        matched_any = False
        for telegram_id, watched_ids in watches_by_person.items():
            person = allowlist.get(telegram_id)
            if person is None or not allowlist.is_allowlisted(telegram_id):
                continue
            searches = [
                search
                for search in person.profile.active_searches()
                if search.search_id in watched_ids and matches_search(event.listing, search)
            ]
            if not searches:
                continue
            matched_any = True
            already_seen = {
                upgrade_seen_key(key) for key in store.seen_keys(person.telegram_id)
            }
            if event.identity_key in already_seen:
                report.legacy_seen_suppressed += 1
                continue
            matching_ids = [search.search_id for search in searches if search.search_id]
            outcome = None
            for search in searches:
                if not search.search_id:  # schema-v2 planner already excludes these
                    continue
                message = PlannedPush(
                    telegram_id=person.telegram_id,
                    display_name=person.display_name,
                    search_name=search.name,
                    key=event.identity_key,
                    listing=event.listing,
                ).render()
                message = "Newly observed rental (first seen by CHC)\n" + message
                window_start, window_end = local_day_window_utc(person.profile, now_utc)
                outcome = store.event_store().enqueue_shadow(
                    idempotency_key=_idempotency_key(event, person.telegram_id),
                    telegram_id=person.telegram_id,
                    search_id=search.search_id,
                    primary_search_name=search.name,
                    matching_search_ids=matching_ids,
                    identity_key=event.identity_key,
                    version_hash=event.version_hash,
                    event_type=event.event_type,
                    message_text=message,
                    not_before_utc=notification_not_before(person.profile, now_utc),
                    created_at_utc=now_utc,
                    cap_window_start_utc=window_start,
                    cap_window_end_utc=window_end,
                    daily_cap=search.daily_cap,
                )
                if outcome.outcome != "cap_reached":
                    break
            if outcome is None or outcome.outcome == "cap_reached":
                report.cap_suppressed += 1
            elif outcome.outcome == "duplicate":
                report.duplicates += 1
            else:
                report.queued += 1
        if not matched_any:
            report.unmatched += 1
    return report


def process_incremental_report(
    store: Store,
    report: IncrementalCycleReport,
    *,
    now_utc: datetime,
) -> IncrementalProcessingReport:
    processed = IncrementalProcessingReport()
    for collection in report.collections:
        if collection.status != "succeeded" or not collection.run_id:
            continue
        observed = observe_collection(store, collection, now_utc=now_utc)
        processed.observations.append(observed)
        shadow = plan_shadow_notifications(
            store,
            events=observed.events,
            query_id=observed.query_id,
            now_utc=now_utc,
        )
        processed.outbox.queued += shadow.queued
        processed.outbox.duplicates += shadow.duplicates
        processed.outbox.cap_suppressed += shadow.cap_suppressed
        processed.outbox.legacy_seen_suppressed += shadow.legacy_seen_suppressed
        processed.outbox.unmatched += shadow.unmatched
        store.event_store().mark_source_run_processed(
            collection.run_id, now_utc=now_utc
        )
    return processed
