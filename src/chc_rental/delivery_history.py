"""Typed records for the append-only routine Telegram delivery journal.

The journal is intentionally file-backed like the daily pipeline.  It records
one exact outbound message per attempt and folds append-only state transitions
into the operator-facing diary.  The existing seen ledger remains a compact
90-day suppression projection; accepted journal rows are the durable evidence
that can repair that projection after a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable


ROUTINE_STATUSES = frozenset(
    {"prepared", "sending", "accepted", "failed", "uncertain"}
)
DELIVERY_CHANNELS = frozenset({"scheduled", "test", "incremental"})


@dataclass(frozen=True)
class RoutineDeliveryItem:
    key: str
    search_name: str
    url: str
    address: str | None = None
    unit: str | None = None
    city: str | None = None
    district: str | None = None
    price: int | None = None
    beds: int | None = None
    baths: float | None = None
    sqft: int | None = None
    source: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RoutineDeliveryItem":
        return cls(
            key=str(raw.get("key") or ""),
            search_name=str(raw.get("search_name") or ""),
            url=str(raw.get("url") or ""),
            address=_optional_text(raw.get("address")),
            unit=_optional_text(raw.get("unit")),
            city=_optional_text(raw.get("city")),
            district=_optional_text(raw.get("district")),
            price=_optional_int(raw.get("price")),
            beds=_optional_int(raw.get("beds")),
            baths=_optional_float(raw.get("baths")),
            sqft=_optional_int(raw.get("sqft")),
            source=_optional_text(raw.get("source")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "search_name": self.search_name,
            "url": self.url,
            "address": self.address,
            "unit": self.unit,
            "city": self.city,
            "district": self.district,
            "price": self.price,
            "beds": self.beds,
            "baths": self.baths,
            "sqft": self.sqft,
            "source": self.source,
        }

    @property
    def display_name(self) -> str:
        if self.address:
            return self.address + (f", {self.unit}" if self.unit else "")
        return self.key or "Legacy listing"


@dataclass(frozen=True)
class RoutineDeliveryEntry:
    attempt_id: str
    attempt_key: str
    telegram_id: int
    display_name: str
    timezone: str
    local_date: date
    kind: str
    status: str
    message_text: str
    items: tuple[RoutineDeliveryItem, ...]
    prepared_at: datetime
    updated_at: datetime
    telegram_message_id: str | None = None
    chat_id: str | None = None
    error: str | None = None
    legacy: bool = False
    channel: str = "scheduled"

    @property
    def visible_in_diary(self) -> bool:
        return self.status in {"accepted", "uncertain"}

    @property
    def primary_item(self) -> RoutineDeliveryItem | None:
        return self.items[0] if self.items else None


@dataclass(frozen=True)
class RoutineDeliveryDay:
    local_date: date
    entries: tuple[RoutineDeliveryEntry, ...]

    @property
    def listing_count(self) -> int:
        return sum(
            len(entry.items)
            for entry in self.entries
            if entry.status == "accepted" and entry.kind == "listing"
        )

    @property
    def notice_count(self) -> int:
        return sum(
            1
            for entry in self.entries
            if entry.status == "accepted" and entry.kind == "notice"
        )

    @property
    def uncertain_count(self) -> int:
        return sum(entry.status == "uncertain" for entry in self.entries)


def fold_routine_events(events: Iterable[dict[str, Any]]) -> list[RoutineDeliveryEntry]:
    """Fold valid append-only events, ignoring damaged or incomplete rows."""
    attempts: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        if not isinstance(event, dict) or event.get("schema_version") != 1:
            continue
        attempt_id = event.get("attempt_id")
        state = event.get("state")
        if not isinstance(attempt_id, str) or state not in ROUTINE_STATUSES | {"annotated"}:
            continue
        if state == "prepared":
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if attempt_id not in attempts:
                order.append(attempt_id)
            attempts[attempt_id] = {
                "attempt_id": attempt_id,
                "attempt_key": str(event.get("attempt_key") or attempt_id),
                "telegram_id": event.get("telegram_id"),
                "prepared_at": event.get("event_at"),
                "updated_at": event.get("event_at"),
                "status": state,
                "payload": payload,
                "telegram_message_id": None,
                "chat_id": None,
                "error": None,
            }
            continue
        current = attempts.get(attempt_id)
        if current is None:
            continue
        if state == "annotated":
            patch = event.get("payload_patch")
            if isinstance(patch, dict):
                for field in ("message_text", "channel"):
                    if field in patch:
                        current["payload"][field] = patch[field]
                if isinstance(patch.get("items"), list):
                    current["payload"]["items"] = patch["items"]
                if "error" in patch:
                    current["error"] = (
                        str(patch["error"]) if patch["error"] is not None else None
                    )
            continue
        if state not in ROUTINE_STATUSES:
            continue
        current["status"] = state
        current["updated_at"] = event.get("event_at") or current["updated_at"]
        if event.get("telegram_message_id") is not None:
            current["telegram_message_id"] = str(event["telegram_message_id"])
        if event.get("chat_id") is not None:
            current["chat_id"] = str(event["chat_id"])
        if event.get("error") is not None:
            current["error"] = str(event["error"])

    entries: list[RoutineDeliveryEntry] = []
    for attempt_id in order:
        current = attempts[attempt_id]
        payload = current["payload"]
        try:
            prepared_at = datetime.fromisoformat(str(current["prepared_at"]))
            updated_at = datetime.fromisoformat(str(current["updated_at"]))
            local_date = date.fromisoformat(str(payload["local_date"]))
            telegram_id = int(current["telegram_id"])
            items_raw = payload.get("items") or []
            if not isinstance(items_raw, list):
                continue
            items = tuple(
                RoutineDeliveryItem.from_dict(item)
                for item in items_raw
                if isinstance(item, dict)
            )
        except (KeyError, TypeError, ValueError):
            continue
        entries.append(
            RoutineDeliveryEntry(
                attempt_id=attempt_id,
                attempt_key=str(current["attempt_key"]),
                telegram_id=telegram_id,
                display_name=str(payload.get("display_name") or telegram_id),
                timezone=str(payload.get("timezone") or "UTC"),
                local_date=local_date,
                kind=str(payload.get("kind") or "listing"),
                status=str(current["status"]),
                message_text=str(payload.get("message_text") or ""),
                items=items,
                prepared_at=prepared_at,
                updated_at=updated_at,
                telegram_message_id=current["telegram_message_id"],
                chat_id=current["chat_id"],
                error=current["error"],
                legacy=bool(payload.get("legacy")),
                channel=(
                    str(payload.get("channel") or "scheduled")
                    if str(payload.get("channel") or "scheduled") in DELIVERY_CHANNELS
                    else "scheduled"
                ),
            )
        )
    return entries


def group_routine_days(
    entries: Iterable[RoutineDeliveryEntry],
) -> list[RoutineDeliveryDay]:
    grouped: dict[date, list[RoutineDeliveryEntry]] = {}
    for entry in entries:
        if entry.visible_in_diary:
            grouped.setdefault(entry.local_date, []).append(entry)
    return [
        RoutineDeliveryDay(
            local_date=day,
            entries=tuple(sorted(grouped[day], key=lambda item: item.updated_at)),
        )
        for day in sorted(grouped, reverse=True)
    ]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DELIVERY_CHANNELS",
    "RoutineDeliveryDay",
    "RoutineDeliveryEntry",
    "RoutineDeliveryItem",
    "fold_routine_events",
    "group_routine_days",
]
