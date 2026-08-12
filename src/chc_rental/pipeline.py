"""Decide what to push, then optionally push it.

Order of operations is deliberate and load-bearing:

    validate -> allowlist -> due -> match -> dedupe -> cap -> send -> mark seen

Three of those steps exist because the previous build got them wrong:

*   **allowlist** is re-checked here, at push time, not merely when config was
    loaded.  The old pipeline selected on a profile's own active flag and never
    joined the allowlist, so people who had been removed kept receiving listings.
*   **dedupe** suppresses within a run as well as across runs, so a person whose
    two searches both match one listing still receives it once.
*   **mark seen** happens only after a confirmed send.  Recording first means a
    failed send is never retried; recording after means the worst case is one
    duplicate, which is the safer direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional, Protocol, Sequence

from pydantic import ValidationError

from chc_rental.dedup import dedup_key, upgrade_seen_key
from chc_rental.matching import matches_search
from chc_rental.models import AllowlistEntry, Listing
from chc_rental.notification_schedule import is_profile_due
from chc_rental.store import Store


class PushSender(Protocol):
    """Outbound-only send boundary. Implementations must raise on failure."""

    def send(self, *, telegram_id: int, text: str) -> Any: ...


@dataclass(frozen=True)
class PlannedPush:
    telegram_id: int
    display_name: str
    search_name: str
    key: str
    listing: Listing

    def render(self) -> str:
        parts = [
            f"{self.listing.address}",
            f"{self.listing.city}"
            + (f" / {self.listing.district}" if self.listing.district else ""),
            f"${self.listing.price:,}",
            f"{self.listing.beds} bed",
            f"{self.listing.baths:g} bath",
        ]
        if self.listing.sqft is not None:
            parts.append(f"{self.listing.sqft} sqft")
        return f"[{self.search_name}] " + " | ".join(parts) + f"\n{self.listing.url}"


@dataclass
class PipelineResult:
    planned: list[PlannedPush] = field(default_factory=list)
    no_results_for: list[int] = field(default_factory=list)
    sent: int = 0
    failed: int = 0
    summary: dict[str, Any] = field(default_factory=dict)

    def preview(self) -> str:
        lines = ["DAILY PUSH PREVIEW"]
        by_person: dict[int, list[PlannedPush]] = {}
        for item in self.planned:
            by_person.setdefault(item.telegram_id, []).append(item)
        for telegram_id, items in sorted(by_person.items()):
            lines.append(f"to={telegram_id} {items[0].display_name} listings={len(items)}")
            for item in items:
                head, _, tail = item.render().partition("\n")
                lines.append(f"  - {head}")
                if tail:
                    lines.append(f"    {tail}")
        for telegram_id in sorted(self.no_results_for):
            lines.append(f"to={telegram_id} (no matches today)")
        return "\n".join(lines)


def validate_records(
    store: Store,
    raw_records: Iterable[Any],
    *,
    day: date,
    source: str,
) -> tuple[list[Listing], int]:
    """Validate raw records, retaining each failure instead of aborting.

    The previous build raised on the first bad record, so one malformed entry
    discarded every good listing alongside it and kept none of them for review.
    """
    listings: list[Listing] = []
    rejected = 0
    for raw in raw_records:
        try:
            listings.append(Listing.model_validate(raw))
        except ValidationError as exc:
            rejected += 1
            record_source = raw.get("source") if isinstance(raw, dict) else None
            store.record_rejected(
                day, source=str(record_source or source), reason=str(exc), raw=raw
            )
    return listings, rejected


def _listing_preference(listing: Listing) -> tuple[int, int]:
    """Prefer a direct Zillow link over an address-only Maps fallback."""
    is_zillow = int(listing.source.strip().lower() == "zillow")
    is_direct = int("google.com/maps/" not in listing.url.lower())
    return is_zillow, is_direct


def _unique(listings: Sequence[Listing]) -> list[Listing]:
    """Collapse cross-source duplicates while retaining the best link."""
    unique: dict[str, Listing] = {}
    for listing in listings:
        key = dedup_key(listing)
        current = unique.get(key)
        if current is None or _listing_preference(listing) > _listing_preference(current):
            unique[key] = listing
    return list(unique.values())


def plan_pushes(
    store: Store,
    listings: Sequence[Listing],
    *,
    now_utc: datetime,
    allow_no_results: bool = True,
) -> PipelineResult:
    """Work out exactly what each allowlisted person should receive."""
    allowlist = store.load_allowlist()
    unique = _unique(listings)
    result = PipelineResult()
    due_count = 0

    for person in allowlist.people:
        # Re-check membership rather than trusting an earlier filter.
        if not allowlist.is_allowlisted(person.telegram_id):
            continue
        last_sent = store.last_sent_at(person.telegram_id)
        if not is_profile_due(person.profile, now_utc, last_sent_at_utc=last_sent):
            continue
        due_count += 1

        # Project legacy v2 keys into the source-independent v3 identity at
        # read time. This avoids both a destructive ledger rewrite and a wave
        # of repeat notifications when the second source is enabled.
        already_seen = {
            upgrade_seen_key(key) for key in store.seen_keys(person.telegram_id)
        }
        planned_this_run: set[str] = set()
        person_items: list[PlannedPush] = []

        for search in person.profile.active_searches():
            taken = 0
            for listing in unique:
                if taken >= search.daily_cap:
                    break
                if not matches_search(listing, search):
                    continue
                key = dedup_key(listing)
                if key in already_seen or key in planned_this_run:
                    continue
                planned_this_run.add(key)
                person_items.append(
                    PlannedPush(
                        telegram_id=person.telegram_id,
                        display_name=person.display_name,
                        search_name=search.name,
                        key=key,
                        listing=listing,
                    )
                )
                taken += 1

        if person_items:
            result.planned.extend(person_items)
        elif person.profile.notify_on_no_results and allow_no_results:
            result.no_results_for.append(person.telegram_id)

    result.summary = {
        "people_total": len(allowlist.people),
        "people_allowlisted": len(allowlist.active_people()),
        "people_due": due_count,
        "listings_in": len(listings),
        "listings_unique": len(unique),
        "planned_pushes": len(result.planned),
        "no_results_suppressed": bool(not allow_no_results),
        "no_results_notices": len(result.no_results_for),
    }
    return result


def deliver(
    store: Store,
    result: PipelineResult,
    *,
    sender: Optional[PushSender],
    live: bool = False,
    now_utc: Optional[datetime] = None,
) -> PipelineResult:
    """Send the planned pushes. Without ``live`` and a sender this is a no-op.

    Pass the same ``now_utc`` that planning used so ledger stamps and the
    due-gate share one clock; None falls back to the wall clock.
    """
    settings = store.load_settings()
    if not live or not settings.live_push_enabled or sender is None:
        result.summary["delivery"] = "dry-run"
        return result

    allowlist = store.load_allowlist()
    for item in result.planned:
        # Last-moment re-check: config may have changed since planning.
        if not allowlist.is_allowlisted(item.telegram_id):
            continue
        try:
            sender.send(telegram_id=item.telegram_id, text=item.render())
        except Exception:
            result.failed += 1
            continue
        # Only now is it safe to record the listing as delivered.
        store.mark_seen(
            item.telegram_id,
            item.key,
            search_name=item.search_name,
            url=item.listing.url,
            now_utc=now_utc,
        )
        result.sent += 1

    # Notices are tallied apart from listing pushes: ``sent``/``failed`` feed
    # the status screen's push counts, and a failed courtesy notice must not
    # masquerade as an undelivered listing.
    notices_sent = 0
    notices_failed = 0
    for telegram_id in result.no_results_for:
        if not allowlist.is_allowlisted(telegram_id):
            continue
        try:
            sender.send(telegram_id=telegram_id, text="No new rentals matched your searches today.")
        except Exception:
            notices_failed += 1
            continue
        # Stamp the ledger so the due-gate advances: without this an hourly
        # runner re-sends the notice every hour until local midnight.
        store.mark_notified(telegram_id, now_utc=now_utc)
        notices_sent += 1

    result.summary["delivery"] = "live"
    result.summary["sent"] = result.sent
    result.summary["failed"] = result.failed
    result.summary["no_results_sent"] = notices_sent
    result.summary["no_results_failed"] = notices_failed
    return result


def person_summary(person: AllowlistEntry) -> str:
    active = len(person.profile.active_searches())
    total = len(person.profile.searches)
    state = "active" if person.active else "removed"
    return f"{person.display_name} ({person.telegram_id}) — {active}/{total} searches, {state}"
