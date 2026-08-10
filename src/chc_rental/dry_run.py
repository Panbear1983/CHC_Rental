"""Fixture-only, read-only orchestration preview.

This module deliberately has no provider, HTTP, scraper, Telegram, daemon, or
write path.  It reads an existing SQLite database in read-only mode and plans a
deterministic notification preview from caller-provided listing fixtures.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from chc_rental.dedup import dedup_key
from chc_rental.matching import matches_profile
from chc_rental.models import Listing, PreferenceProfile
from chc_rental.notification_schedule import is_profile_due
from chc_rental.scheduler import ProfileCandidates, plan_daily_allocation


@dataclass(frozen=True)
class DryRunResult:
    """Read-only preview text and machine-consumable deterministic counts."""

    preview: str
    summary: dict[str, int]


def load_fixture_listings(path: str | Path) -> list[Listing]:
    """Load a JSON list, or an object containing exactly a ``listings`` list."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid fixture JSON: {exc}") from exc
    if isinstance(raw, dict):
        if set(raw) != {"listings"}:
            raise ValueError("fixture object must contain only a 'listings' field")
        raw = raw["listings"]
    if not isinstance(raw, list):
        raise ValueError("fixture must be a JSON list of listings")
    try:
        return [Listing.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise ValueError(f"invalid fixture listing: {exc}") from exc


def _open_read_only(path: str | Path) -> sqlite3.Connection:
    database_path = Path(path).expanduser().resolve()
    if not database_path.is_file():
        raise ValueError(f"database must be an existing file: {database_path}")
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _active_profiles(connection: sqlite3.Connection) -> list[PreferenceProfile]:
    rows = connection.execute(
        "SELECT * FROM preference_profiles WHERE active = 1 ORDER BY id"
    ).fetchall()
    profiles = []
    for row in rows:
        data = dict(row)
        data["property_types"] = json.loads(data["property_types"])
        data["required_features"] = json.loads(data["required_features"])
        data["excluded_features"] = json.loads(data["excluded_features"])
        data["active"] = bool(data["active"])
        profiles.append(PreferenceProfile.model_validate(data))
    return profiles


def _last_sent_at(connection: sqlite3.Connection, profile_id: int) -> datetime | None:
    row = connection.execute(
        "SELECT MAX(updated_at) AS last_sent_at FROM delivery_ledger "
        "WHERE profile_id = ? AND status = 'sent'",
        (profile_id,),
    ).fetchone()
    value = row["last_sent_at"] if row else None
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _eligible_for_delivery(connection: sqlite3.Connection, telegram_user_id: int, listing_key: str) -> bool:
    row = connection.execute(
        "SELECT status, attempt_count, max_attempts FROM delivery_ledger "
        "WHERE telegram_user_id = ? AND listing_key = ?",
        (telegram_user_id, listing_key),
    ).fetchone()
    if row is None or row["status"] == "pending":
        return True
    if row["status"] == "sent":
        return False
    return row["attempt_count"] < row["max_attempts"]


def _unique_listings(listings: Sequence[Listing]) -> list[Listing]:
    unique: list[Listing] = []
    seen: set[str] = set()
    for listing in listings:
        key = dedup_key(listing)
        if key not in seen:
            seen.add(key)
            unique.append(listing)
    return unique


def _render_preview(allocation: dict[int, list[Listing]], profiles: dict[int, PreferenceProfile], summary: dict[str, int]) -> str:
    lines = ["DRY-RUN NOTIFICATION PREVIEW"]
    for profile_id in sorted(profiles):
        profile = profiles[profile_id]
        listings = allocation.get(profile_id, [])
        if not listings:
            if profile.notify_on_no_results:
                lines.append(
                    f"profile={profile_id} name={profile.profile_name} "
                    "listings=0 no-results-notification=yes"
                )
            continue
        lines.append(f"profile={profile_id} name={profile.profile_name} listings={len(listings)}")
        for listing in listings:
            lines.append(f"- {dedup_key(listing)} | {listing.address} | {listing.city} | ${listing.price}")
    lines.append("SUMMARY " + json.dumps(summary, sort_keys=True))
    return "\n".join(lines)


def run_dry_run(
    database_path: str | Path,
    fixture_listings: Sequence[Listing | dict[str, Any]],
    *,
    now_utc: datetime,
    global_daily_budget: int,
) -> DryRunResult:
    """Return a dry-run plan without writing ledger/budget/delivery state."""
    if global_daily_budget < 0:
        raise ValueError("global_daily_budget must be non-negative")
    try:
        listings = [Listing.model_validate(item) for item in fixture_listings]
    except ValidationError as exc:
        raise ValueError(f"invalid fixture listing: {exc}") from exc
    unique = _unique_listings(listings)

    connection = _open_read_only(database_path)
    try:
        active = _active_profiles(connection)
        due = [
            profile for profile in active
            if is_profile_due(profile, now_utc, last_sent_at_utc=_last_sent_at(connection, profile.id))
        ]
        candidates = {
            profile.id: [
                listing for listing in unique
                if matches_profile(listing, profile)
                and _eligible_for_delivery(connection, profile.telegram_user_id, dedup_key(listing))
            ]
            for profile in due
        }
        allocation = plan_daily_allocation(
            [ProfileCandidates(profile.id, profile.daily_cap, candidates[profile.id]) for profile in due],
            global_daily_budget=global_daily_budget,
        )
    finally:
        connection.close()

    summary = {
        "active_profiles": len(active),
        "allocated_listings": sum(len(items) for items in allocation.values()),
        "due_profiles": len(due),
        "eligible_candidates": sum(len(items) for items in candidates.values()),
        "fixture_listings": len(listings),
        "unique_listings": len(unique),
    }
    profiles_by_id = {profile.id: profile for profile in due}
    no_results_notifications = sum(
        1
        for profile_id, profile in profiles_by_id.items()
        if profile.notify_on_no_results and not allocation.get(profile_id)
    )
    if no_results_notifications:
        summary["no_results_notifications"] = no_results_notifications
    return DryRunResult(_render_preview(allocation, profiles_by_id, summary), summary)


def _parse_now(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--now must be ISO-8601, e.g. 2026-01-15T14:00:00Z") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--now must include a UTC offset")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only fixture-only CHC Rental notification preview")
    parser.add_argument("--db", required=True, help="existing SQLite database path")
    parser.add_argument("--fixture", required=True, help="JSON listing fixture path")
    parser.add_argument("--now", required=True, type=_parse_now, help="timezone-aware ISO-8601 instant")
    parser.add_argument("--global-daily-budget", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        result = run_dry_run(
            args.db,
            load_fixture_listings(args.fixture),
            now_utc=args.now,
            global_daily_budget=args.global_daily_budget,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(result.preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
