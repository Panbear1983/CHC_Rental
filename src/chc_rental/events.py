"""Canonical listing observation and deterministic event classification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from chc_rental.dedup import dedup_key
from chc_rental.event_store import ObservationInput
from chc_rental.incremental import IncrementalCollection
from chc_rental.models import Listing
from chc_rental.pipeline import validate_records
from chc_rental.store import Store


@dataclass(frozen=True)
class ListingEvent:
    query_id: str
    run_id: str
    event_type: str
    identity_key: str
    version_hash: str
    listing: Listing


@dataclass
class ObservationReport:
    query_id: str
    run_id: str
    baseline_established: bool = False
    validated: int = 0
    rejected: int = 0
    events: list[ListingEvent] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
        return {
            "query_id": self.query_id,
            "run_id": self.run_id,
            "baseline_established": self.baseline_established,
            "validated": self.validated,
            "rejected": self.rejected,
            "events": counts,
        }


def material_version_hash(listing: Listing) -> str:
    """Hash only canonical fields whose change is operationally meaningful."""
    material = {
        "address": listing.address,
        "unit": listing.unit,
        "city": listing.city,
        "state": listing.state,
        "district": listing.district,
        "price": listing.price,
        "property_type": listing.property_type.value,
        "beds": listing.beds,
        "baths": listing.baths,
        "sqft": listing.sqft,
        "url": listing.url,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _canonical_json(listing: Listing) -> str:
    return json.dumps(
        listing.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def observe_collection(
    store: Store,
    collection: IncrementalCollection,
    *,
    now_utc: datetime,
) -> ObservationReport:
    if collection.status != "succeeded" or not collection.run_id:
        raise ValueError("only a successful persisted collection can be observed")
    listings, rejected = validate_records(
        store,
        collection.records,
        day=now_utc.date(),
        source="zillow-incremental",
    )
    unique: dict[tuple[str, str], Listing] = {}
    for listing in listings:
        identity = dedup_key(listing)
        version = material_version_hash(listing)
        unique.setdefault((identity, version), listing)
    prepared = [
        ObservationInput(
            identity_key=identity,
            version_hash=version,
            canonical_json=_canonical_json(listing),
            source=listing.source,
            source_listing_id=listing.source_listing_id,
            source_url=listing.url,
            source_posted_at=(
                listing.first_seen_at.isoformat() if listing.first_seen_at is not None else None
            ),
        )
        for (identity, version), listing in unique.items()
    ]
    event_store = store.event_store()
    baseline_was_pending = (
        event_store.query_baseline_states().get(collection.query_id) != "established"
    )
    outcomes = event_store.record_observations(
        run_id=collection.run_id,
        items=prepared,
        now_utc=now_utc,
    )
    report = ObservationReport(
        query_id=collection.query_id,
        run_id=collection.run_id,
        baseline_established=baseline_was_pending,
        validated=len(unique),
        rejected=rejected,
    )
    for outcome in outcomes:
        listing = unique[(outcome.identity_key, outcome.version_hash)]
        report.events.append(
            ListingEvent(
                query_id=collection.query_id,
                run_id=collection.run_id,
                event_type=outcome.event_type,
                identity_key=outcome.identity_key,
                version_hash=outcome.version_hash,
                listing=listing,
            )
        )
    return report
