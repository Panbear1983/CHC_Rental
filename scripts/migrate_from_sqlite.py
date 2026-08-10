"""One-shot migration from the retired SQLite build into config/allowlist.yaml.

Each old `preference_profiles` row was one search, so it becomes one `Search`
under its owner's single profile.  Delivery time and timezone were per-profile
before and are per-person now; the first profile's values win and any others are
reported so nothing changes silently.

Run once, check the output, then delete this file.

    python scripts/migrate_from_sqlite.py data/chc_rental.sqlite3 --root .
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chc_rental.models import Allowlist, AllowlistEntry, Profile, Search  # noqa: E402
from chc_rental.store import Store  # noqa: E402


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: Path, root: Path) -> Allowlist:
    conn = sqlite3.connect(f"file:{db_path.resolve().as_uri()[7:]}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    profile_columns = _column_names(conn, "preference_profiles")
    has_sqft = {"sqft_min", "sqft_max"} <= profile_columns
    has_notify = "notify_on_no_results" in profile_columns

    people: list[AllowlistEntry] = []
    notes: list[str] = []

    for user in conn.execute("SELECT * FROM allowlisted_users ORDER BY id"):
        rows = conn.execute(
            "SELECT * FROM preference_profiles WHERE telegram_user_id = ? ORDER BY id",
            (user["telegram_user_id"],),
        ).fetchall()

        searches: list[Search] = []
        delivery_time = "09:00"
        timezone_name = "America/New_York"
        notify = False

        for index, row in enumerate(rows):
            if index == 0:
                delivery_time = row["delivery_time"]
                timezone_name = row["timezone"]
                notify = bool(row["notify_on_no_results"]) if has_notify else False
            elif (row["delivery_time"], row["timezone"]) != (delivery_time, timezone_name):
                notes.append(
                    f"user {user['telegram_user_id']}: profile {row['profile_name']!r} had "
                    f"{row['delivery_time']} {row['timezone']}, kept {delivery_time} {timezone_name}"
                )
            searches.append(
                Search(
                    name=row["profile_name"],
                    active=bool(row["active"]),
                    city=row["city"],
                    district=row["district"],
                    price_min=row["price_min"],
                    price_max=row["price_max"],
                    property_types=json.loads(row["property_types"]),
                    bed_min=row["bed_min"],
                    bed_max=row["bed_max"],
                    bath_min=row["bath_min"],
                    bath_max=row["bath_max"],
                    sqft_min=row["sqft_min"] if has_sqft else None,
                    sqft_max=row["sqft_max"] if has_sqft else None,
                    required_features=json.loads(row["required_features"]),
                    excluded_features=json.loads(row["excluded_features"]),
                    daily_cap=row["daily_cap"],
                )
            )

        people.append(
            AllowlistEntry(
                telegram_id=user["telegram_user_id"],
                display_name=user["display_name"],
                active=bool(user["active"]),
                profile=Profile(
                    delivery_time=delivery_time,
                    timezone=timezone_name,
                    notify_on_no_results=notify,
                    searches=searches,
                ),
            )
        )
    conn.close()

    allowlist = Allowlist(people=people)
    store = Store(root)
    store.initialize()
    store.save_allowlist(allowlist)

    print(f"migrated {len(people)} person(s) into {store.allowlist_path}")
    for person in people:
        print(f"  {person.telegram_id} {person.display_name} — {len(person.profile.searches)} search(es)")
    for note in notes:
        print(f"  note: {note}")
    return allowlist


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="Path to the retired chc_rental.sqlite3")
    parser.add_argument("--root", default=".", help="Target data directory")
    args = parser.parse_args()
    db_path = Path(args.database)
    if not db_path.is_file():
        parser.error(f"database must be an existing file: {db_path}")
    migrate(db_path, Path(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
