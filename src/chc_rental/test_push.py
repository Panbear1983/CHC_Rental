"""Manual, selected-recipient Telegram test pushes.

This path renders the selected person's saved preferences against the most
recent compatible Zillow cache and never starts a paid source request.  Its
confirmed Telegram receipts share the same per-recipient delivery bank as the
scheduled paths, while its events are excluded from the morning due-time gate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from chc_rental.dedup import dedup_key, upgrade_seen_key
from chc_rental.matching import matches_search
from chc_rental.models import Allowlist, AllowlistEntry, Listing, Search
from chc_rental.notify.telegram import TelegramReceipt, TelegramSendError
from chc_rental.pipeline import PlannedPush
from chc_rental.sources.planner import plan_queries
from chc_rental.store import Store

TELEGRAM_MESSAGE_LIMIT = 4096


class TestPushBlocked(ValueError):
    """The selected recipient is not currently eligible for a test push."""


@dataclass(frozen=True)
class TestPushPlan:
    telegram_id: int
    display_name: str
    preference_count: int
    preference_fingerprint: str
    messages: tuple[str, ...]
    listing_count: int = 0
    cache_date: str | None = None
    part_items: tuple[tuple["TestPushListing", ...], ...] = ()
    repeat_override: bool = False

    @property
    def confirmation_text(self) -> str:
        noun = "preference" if self.preference_count == 1 else "preferences"
        parts = "" if len(self.messages) == 1 else f" in {len(self.messages)} parts"
        cache = f" from the {self.cache_date} Zillow cache" if self.cache_date else ""
        listing_noun = "listing" if self.listing_count == 1 else "listings"
        base = (
            f"Send one real Telegram test push{parts} to {self.display_name} "
            f"({self.telegram_id}) containing all {self.preference_count} active {noun} "
            f"and {self.listing_count} matching Zillow {listing_noun}{cache}?"
        )
        if self.repeat_override:
            return (
                f"WARNING — REPEAT SEND. {base} This is audited; the original 90-day "
                "clock and next push time are unchanged."
            )
        return (
            f"{base} Successful listings are banked 90 days; next push time is unchanged."
        )


@dataclass(frozen=True)
class TestPushListing:
    """One normalized property identity carried by a Telegram message part."""

    key: str
    search_name: str
    url: str


@dataclass(frozen=True)
class _MessageUnit:
    text: str
    items: tuple[TestPushListing, ...] = ()


@dataclass(frozen=True)
class TestPushResult:
    status: str
    telegram_id: int
    display_name: str
    preference_count: int
    parts_total: int
    parts_accepted: int
    message_ids: tuple[str, ...] = ()
    chat_ids: tuple[str, ...] = ()
    error: str | None = None
    audit_path: str | None = None
    listing_count: int = 0
    listing_keys: tuple[str, ...] = ()

    @property
    def dashboard_message(self) -> str:
        if self.status == "success":
            ids = ", ".join(self.message_ids)
            return (
                f"Test push delivered to {self.display_name} ({self.telegram_id}); "
                f"{self.listing_count} cached Zillow listing(s); Telegram receipt {ids}."
            )
        if self.status == "partial":
            return (
                f"Test push partially delivered to {self.display_name}: "
                f"{self.parts_accepted}/{self.parts_total} parts accepted; {self.error}"
            )
        if self.status == "uncertain":
            return f"Test push acceptance is unknown for {self.display_name}: {self.error}"
        return f"Test push not delivered to {self.display_name}: {self.error}"


# These are service records, not pytest test containers when imported by tests.
TestPushBlocked.__test__ = False
TestPushPlan.__test__ = False
TestPushListing.__test__ = False
TestPushResult.__test__ = False


class TestPushSender(Protocol):
    def send(self, *, telegram_id: int, text: str) -> Any: ...


def _active_person(store: Store, telegram_id: int) -> tuple[AllowlistEntry, list[Search]]:
    allowlist = store.load_allowlist()
    person = allowlist.get(telegram_id)
    if person is None:
        raise TestPushBlocked(f"Telegram ID {telegram_id} is not on the allowlist")
    if not allowlist.is_allowlisted(telegram_id):
        raise TestPushBlocked(f"{person.display_name} is paused; resume them before testing")
    searches = person.profile.active_searches()
    if not searches:
        raise TestPushBlocked(f"{person.display_name} has no active preferences to send")
    return person, searches


def _fingerprint(person: AllowlistEntry, searches: list[Search]) -> str:
    payload = {
        "telegram_id": person.telegram_id,
        "display_name": person.display_name,
        "active_preferences": [search.model_dump(mode="json") for search in searches],
    }
    stable = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _header(
    person: AllowlistEntry,
    count: int,
    listing_count: int,
    cache_date: date,
    part: int,
    total: int,
) -> str:
    part_text = "" if total == 1 else f" (part {part}/{total})"
    return (
        f"CHC Rental — MANUAL TEST PUSH{part_text}\n"
        f"For: {person.display_name} ({person.telegram_id})\n"
        f"Normal push time: {person.profile.delivery_time} {person.profile.timezone}\n"
        f"Active preferences: {count}\n"
        f"Matching cached Zillow listings: {listing_count}\n"
        f"Zillow cache date: {cache_date.isoformat()}"
    )


def _expected_query_scope(person: AllowlistEntry) -> list[dict[str, str]]:
    # Manual Test push is selected-recipient only. Another member adding or
    # editing an unrelated city must never invalidate this person's preview.
    queries, _ = plan_queries(Allowlist(people=[person]))
    return sorted(
        (
            {"city": query.city.strip().casefold(), "state": query.state.strip().upper()}
            for query in queries
        ),
        key=lambda item: (item["city"], item["state"]),
    )


def _latest_zillow_cache(
    store: Store,
    *,
    person: AllowlistEntry,
    now_utc: datetime,
) -> tuple[date, list[Listing]]:
    expected_scope = _expected_query_scope(person)
    if not expected_scope:
        raise TestPushBlocked("no fetchable active preferences are configured")
    retention_days = store.load_settings().cache_retention_days
    expected_keys = {
        (item["city"].strip().casefold(), item["state"].strip().upper())
        for item in expected_scope
    }
    candidates: list[tuple[date, Any]] = []
    for path in store.state_dir.glob("cache/*/zillow.json"):
        try:
            cache_date = date.fromisoformat(path.parent.name)
        except ValueError:
            continue
        age = (now_utc.date() - cache_date).days
        if age < 0 or age > retention_days:
            continue
        metadata = store.load_cache_metadata(cache_date, "zillow")
        cached_scope = metadata.get("query_scope") if metadata else None
        if not isinstance(cached_scope, list):
            continue
        try:
            cached_keys = {
                (
                    str(item["city"]).strip().casefold(),
                    str(item["state"]).strip().upper(),
                )
                for item in cached_scope
                if isinstance(item, dict)
            }
        except (KeyError, TypeError):
            continue
        # Test push never starts a source request, so a retained cache covering
        # MORE cities than the current allowlist is safe: local matching still
        # applies the selected person's exact filters. A narrower cache is not
        # safe because it may silently omit one of the required cities.
        if not expected_keys.issubset(cached_keys):
            continue
        raw = store.load_cached(cache_date, "zillow")
        if isinstance(raw, list):
            candidates.append((cache_date, raw))
    if not candidates:
        raise TestPushBlocked(
            "no compatible Zillow cache is available for the current active cities. "
            "Run the normal fetch after repairing APIFY_TOKEN, then retry Test push."
        )
    cache_date, raw_records = max(candidates, key=lambda item: item[0])
    listings: list[Listing] = []
    for raw in raw_records:
        try:
            listing = Listing.model_validate(raw)
        except ValidationError:
            continue
        if listing.source.strip().lower() == "zillow":
            listings.append(listing)
    return cache_date, listings


def _matches_by_search(
    searches: list[Search],
    listings: list[Listing],
    *,
    excluded_keys: set[str] | None = None,
) -> list[tuple[Search, list[Listing]]]:
    excluded = excluded_keys or set()
    selected: list[tuple[Search, list[Listing]]] = []
    for search in searches:
        matches: list[Listing] = []
        for listing in listings:
            if len(matches) >= search.daily_cap:
                break
            if not matches_search(listing, search):
                continue
            if dedup_key(listing) in excluded:
                continue
            matches.append(listing)
        selected.append((search, matches))
    return selected


def _message_units(
    person: AllowlistEntry,
    matches_by_search: list[tuple[Search, list[Listing]]],
) -> list[_MessageUnit]:
    # Aggregate overlapping preferences onto one card. The recipient gets each
    # property link once, while every preference that selected it remains
    # visible in the bracketed label.
    cards: dict[str, tuple[Listing, list[str]]] = {}
    empty_searches: list[Search] = []
    for search, listings in matches_by_search:
        if not listings:
            empty_searches.append(search)
            continue
        for listing in listings:
            key = dedup_key(listing)
            if key not in cards:
                cards[key] = (listing, [])
            names = cards[key][1]
            if search.name not in names:
                names.append(search.name)
    units: list[_MessageUnit] = []
    for key, (listing, names) in cards.items():
        search_name = " / ".join(names)
        units.append(
            _MessageUnit(
                text=PlannedPush(
                    telegram_id=person.telegram_id,
                    display_name=person.display_name,
                    search_name=search_name,
                    key=key,
                    listing=listing,
                ).render(),
                items=(
                    TestPushListing(
                        key=key,
                        search_name=search_name,
                        url=listing.url,
                    ),
                ),
            )
        )
    units.extend(
        _MessageUnit(
            text=(
                f"[{search.name}] No matching listing was found in the compatible "
                "Zillow cache."
            )
        )
        for search in empty_searches
    )
    return units


def _render_messages(
    person: AllowlistEntry,
    searches: list[Search],
    matches_by_search: list[tuple[Search, list[Listing]]],
    *,
    cache_date: date,
    listing_count: int,
    max_chars: int,
) -> tuple[tuple[str, ...], tuple[tuple[TestPushListing, ...], ...]]:
    if max_chars < 512:
        raise ValueError("test-push message limit must be at least 512 characters")
    sections = _message_units(person, matches_by_search)
    footer = (
        f"Manual test using cached Zillow data from {cache_date.isoformat()} — "
        "no new Zillow fetch and no effect on normal morning delivery."
    )
    # Reserve a conservative multipart header before the number of parts is known.
    overhead = (
        len(_header(person, len(searches), listing_count, cache_date, 999, 999))
        + len(footer)
        + 4
    )
    capacity = max_chars - overhead
    groups: list[list[_MessageUnit]] = []
    current: list[_MessageUnit] = []
    current_length = 0
    for section in sections:
        if len(section.text) > capacity:
            raise TestPushBlocked(
                "one preference is too long for Telegram; shorten its name or feature lists"
            )
        separator = 2 if current else 0
        if current and current_length + separator + len(section.text) > capacity:
            groups.append(current)
            current = []
            current_length = 0
        if current:
            current_length += 2
        current.append(section)
        current_length += len(section.text)
    if current:
        groups.append(current)

    total = len(groups)
    messages = tuple(
        _header(person, len(searches), listing_count, cache_date, part, total)
        + "\n\n"
        + "\n\n".join(unit.text for unit in group)
        + "\n\n"
        + footer
        for part, group in enumerate(groups, 1)
    )
    if any(len(message) > max_chars for message in messages):
        raise TestPushBlocked("listing batch exceeds Telegram's message limit")
    part_items = tuple(
        tuple(item for unit in group for item in unit.items)
        for group in groups
    )
    return messages, part_items


def prepare_test_push(
    store: Store,
    telegram_id: int,
    *,
    max_message_chars: int = TELEGRAM_MESSAGE_LIMIT,
    now_utc: datetime | None = None,
) -> TestPushPlan:
    person, searches = _active_person(store, telegram_id)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cache_date, listings = _latest_zillow_cache(store, person=person, now_utc=now)
    already_seen = {
        upgrade_seen_key(key)
        for key in store.active_seen_keys(telegram_id, now_utc=now)
    }
    # Filter the complete compatible cache before applying each preference's
    # daily cap.  Seen rows at the top of provider order must not crowd newer,
    # unseen rows out of the Test Push batch.
    selected = _matches_by_search(
        searches,
        listings,
        excluded_keys=already_seen,
    )
    listing_count = len(
        {dedup_key(listing) for _, items in selected for listing in items}
    )
    repeat_override = False
    if listing_count == 0:
        matching_including_seen = _matches_by_search(searches, listings)
        matching_seen_count = len(
            {
                dedup_key(listing)
                for _, items in matching_including_seen
                for listing in items
            }
        )
        if matching_seen_count:
            selected = matching_including_seen
            listing_count = matching_seen_count
            repeat_override = True
    messages, part_items = _render_messages(
        person,
        searches,
        selected,
        cache_date=cache_date,
        listing_count=listing_count,
        max_chars=max_message_chars,
    )
    return TestPushPlan(
        telegram_id=person.telegram_id,
        display_name=person.display_name,
        preference_count=len(searches),
        preference_fingerprint=_fingerprint(person, searches),
        messages=messages,
        listing_count=listing_count,
        cache_date=cache_date.isoformat(),
        part_items=part_items,
        repeat_override=repeat_override,
    )


def _receipt(result: Any) -> tuple[str | None, str | None]:
    if isinstance(result, TelegramReceipt):
        return result.message_id, result.chat_id
    if isinstance(result, dict):
        message_id = result.get("message_id")
        chat_id = result.get("chat_id")
        return (
            str(message_id) if message_id is not None else None,
            str(chat_id) if chat_id is not None else None,
        )
    return None, None


def _audited(
    store: Store,
    plan: TestPushPlan,
    result: TestPushResult,
    *,
    now_utc: datetime,
) -> TestPushResult:
    record = {
        "attempt_id": str(uuid4()),
        "telegram_id": plan.telegram_id,
        "display_name": plan.display_name,
        "preference_count": plan.preference_count,
        "listing_count": plan.listing_count,
        "cache_date": plan.cache_date,
        "preference_fingerprint": plan.preference_fingerprint,
        "parts_total": result.parts_total,
        "parts_accepted": result.parts_accepted,
        "status": result.status,
        "message_ids": list(result.message_ids),
        "chat_ids": list(result.chat_ids),
        "listing_keys": list(result.listing_keys),
        "repeat_override": plan.repeat_override,
        "error": result.error,
    }
    try:
        path = store.record_test_push(now_utc, record)
    except Exception as exc:
        error = f"{result.error + '; ' if result.error else ''}audit write failed: {exc}"
        status = "uncertain" if result.status == "success" else result.status
        return replace(result, status=status, error=error)
    return replace(result, audit_path=str(path))


def deliver_test_push(
    store: Store,
    plan: TestPushPlan,
    *,
    sender: TestPushSender | None,
    now_utc: datetime | None = None,
) -> TestPushResult:
    """Deliver a confirmed plan once, banking only receipt-backed parts."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    message_ids: list[str] = []
    chat_ids: list[str] = []
    delivered_keys: list[str] = []

    def finish(status: str, error: str | None = None) -> TestPushResult:
        return _audited(
            store,
            plan,
            TestPushResult(
                status=status,
                telegram_id=plan.telegram_id,
                display_name=plan.display_name,
                preference_count=plan.preference_count,
                parts_total=len(plan.messages),
                parts_accepted=len(message_ids),
                message_ids=tuple(message_ids),
                chat_ids=tuple(chat_ids),
                error=error,
                listing_count=plan.listing_count,
                listing_keys=tuple(delivered_keys),
            ),
            now_utc=now,
        )

    if sender is None:
        return finish("failed", "no usable Telegram bot token is configured")

    if plan.listing_count > 0 and not plan.part_items:
        return finish("failed", "test push listing map is missing; preview again")
    if plan.part_items and len(plan.part_items) != len(plan.messages):
        return finish("failed", "test push payload changed; preview again")
    part_items = plan.part_items or tuple(() for _ in plan.messages)

    with store.recipient_delivery_lock(plan.telegram_id):
        planned_keys = {
            item.key for items in part_items for item in items
        }
        current_seen = {
            upgrade_seen_key(key)
            for key in store.active_seen_keys(plan.telegram_id, now_utc=now)
        }
        if plan.repeat_override:
            if planned_keys and not planned_keys.issubset(current_seen):
                return finish(
                    "failed",
                    "delivery history changed after preview; preview again",
                )
        elif planned_keys.intersection(current_seen):
            return finish(
                "failed",
                "delivery history changed after preview; preview again",
            )

        for part_index, message in enumerate(plan.messages):
            try:
                live_enabled = store.load_settings().live_push_enabled
            except Exception as exc:
                status = "partial" if message_ids else "failed"
                return finish(status, f"settings could not be read: {exc}")
            if not live_enabled:
                status = "partial" if message_ids else "failed"
                return finish(status, "live delivery was disabled before send")
            try:
                person, searches = _active_person(store, plan.telegram_id)
            except TestPushBlocked as exc:
                status = "partial" if message_ids else "failed"
                return finish(status, str(exc))
            except Exception as exc:
                status = "partial" if message_ids else "failed"
                return finish(status, f"allowlist could not be read: {exc}")
            if _fingerprint(person, searches) != plan.preference_fingerprint:
                status = "partial" if message_ids else "failed"
                return finish(
                    status,
                    "recipient or active preferences changed; confirm again",
                )

            try:
                raw_receipt = sender.send(
                    telegram_id=plan.telegram_id,
                    text=message,
                )
            except TelegramSendError as exc:
                if message_ids:
                    status = "partial"
                else:
                    status = "uncertain" if exc.ambiguous else "failed"
                return finish(status, str(exc))
            except Exception as exc:
                status = "partial" if message_ids else "uncertain"
                return finish(status, f"{type(exc).__name__}: {exc}")

            message_id, chat_id = _receipt(raw_receipt)
            if message_id is None or chat_id != str(plan.telegram_id):
                status = "partial" if message_ids else "uncertain"
                return finish(
                    status,
                    "Telegram accepted the request without a verifiable recipient receipt",
                )
            message_ids.append(message_id)
            chat_ids.append(chat_id)
            try:
                for item in part_items[part_index]:
                    recorded = store.record_delivery(
                        plan.telegram_id,
                        item.key,
                        search_name=item.search_name,
                        url=item.url,
                        now_utc=now,
                        channel="test",
                        telegram_message_id=message_id,
                        chat_id=chat_id,
                        repeat_override=plan.repeat_override,
                    )
                    if not recorded:
                        raise RuntimeError(
                            "listing entered delivery history during transport"
                        )
                    delivered_keys.append(item.key)
            except Exception as exc:
                status = "partial" if len(message_ids) < len(plan.messages) else "uncertain"
                return finish(
                    status,
                    f"delivery bank write failed after Telegram acceptance: {exc}",
                )

    return finish("success")


__all__ = [
    "TestPushBlocked",
    "TestPushListing",
    "TestPushPlan",
    "TestPushResult",
    "deliver_test_push",
    "prepare_test_push",
]
