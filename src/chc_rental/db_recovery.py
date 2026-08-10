"""Bounded local SQLite health checks and owner-approved recovery.

This module never contacts providers and never operates on a configured runtime
path implicitly: callers must name a database file.  It will only mutate a
readable database whose schema is an exact, known legacy CHC schema.  All other
failures require an owner's manual decision.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence, cast

from chc_rental.db import Database, _PREFERENCE_PROFILE_COLUMN_MIGRATIONS

_MANAGED_TABLES = frozenset(
    {
        "allowlisted_users",
        "preference_profiles",
        "audit_events",
        "delivery_ledger",
        "daily_budget_ledger",
    }
)
_LEGACY_MIGRATION_COLUMNS = frozenset(column for column, _ in _PREFERENCE_PROFILE_COLUMN_MIGRATIONS)


@dataclass(frozen=True)
class RecoveryReport:
    """Machine-readable local audit output for a health or recovery action."""

    action: str
    path: str
    status: str
    integrity: str
    schema: str
    owner_action_needed: bool
    detail: str
    backup_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in conn.execute(f"PRAGMA table_info({table})"))


def _table_signature(conn: sqlite3.Connection, table: str) -> tuple[object, ...]:
    """Compare semantic schema details while accepting reviewed ALTER order."""
    columns = tuple(sorted(tuple(column[1:]) for column in _table_columns(conn, table)))
    foreign_keys = tuple(sorted(tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})")))
    unique_indexes = []
    for index in conn.execute(f"PRAGMA index_list({table})"):
        if index[2]:
            unique_indexes.append(
                tuple(sorted(row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})")))
            )
    return columns, foreign_keys, tuple(sorted(unique_indexes))


def _current_schema() -> dict[str, tuple[object, ...]]:
    """Build the expected fingerprint using the application's own schema code."""
    db = Database(":memory:")
    try:
        return {table: _table_signature(db.connection, table) for table in _MANAGED_TABLES}
    finally:
        db.close()


def _legacy_profile_signature(current: tuple[object, ...]) -> tuple[object, ...]:
    """Remove only the four approved migration fields from a table signature."""
    columns = cast(tuple[tuple[object, ...], ...], current[0])
    foreign_keys = current[1]
    unique_indexes = current[2]
    legacy_columns = tuple(column for column in columns if column[0] not in _LEGACY_MIGRATION_COLUMNS)
    return legacy_columns, foreign_keys, unique_indexes


def _schema_state(conn: sqlite3.Connection) -> tuple[str, str]:
    expected = _current_schema()
    found = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if row[0] != "sqlite_sequence"
    }
    if found == _MANAGED_TABLES and all(_table_signature(conn, table) == expected[table] for table in _MANAGED_TABLES):
        return "current", "schema matches the current CHC schema"

    legacy_profile = _legacy_profile_signature(expected["preference_profiles"])
    # This exact old layout is the only schema mutation this tool permits.
    if found == {"allowlisted_users", "preference_profiles"} and (
        _table_signature(conn, "allowlisted_users") == expected["allowlisted_users"]
        and _table_signature(conn, "preference_profiles") == legacy_profile
    ):
        return "known_legacy_migration", "only reviewed preference-profile columns are absent"
    return "unknown", "schema differs from reviewed CHC layouts; no mutation is allowed"


def _owner_report(path: Path, integrity: str, schema: str, detail: str, action: str) -> RecoveryReport:
    return RecoveryReport(
        action=action,
        path=str(path),
        status="owner_action_needed",
        integrity=integrity,
        schema=schema,
        owner_action_needed=True,
        detail=detail,
    )


def check_database(path: str | Path) -> RecoveryReport:
    """Inspect a named SQLite file without changing it."""
    database_path = Path(path)
    if not database_path.is_file():
        return _owner_report(database_path, "unreadable", "unknown", "database file does not exist", "check")
    try:
        conn = sqlite3.connect(f"file:{database_path.absolute()}?mode=ro", uri=True)
        try:
            integrity_rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            if integrity_rows != ["ok"]:
                return _owner_report(
                    database_path, "failed", "unknown", "; ".join(integrity_rows), "check"
                )
            schema, detail = _schema_state(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return _owner_report(database_path, "unreadable", "unknown", str(exc), "check")
    except OSError as exc:
        return _owner_report(database_path, "unreadable", "unknown", str(exc), "check")

    if schema == "current":
        return RecoveryReport("check", str(database_path), "ok", "ok", schema, False, detail)
    if schema == "known_legacy_migration":
        return RecoveryReport("check", str(database_path), "repairable", "ok", schema, False, detail)
    return _owner_report(database_path, "ok", schema, detail, "check")


def _backup_database(path: Path) -> Path:
    """Create a consistent SQLite snapshot before a permitted repair."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.{timestamp}.bak")
    source = sqlite3.connect(f"file:{path.absolute()}?mode=ro", uri=True)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup


def repair_database(path: str | Path) -> RecoveryReport:
    """Backup then apply only the exact reviewed legacy migration.

    Corruption and unfamiliar schemas are refusal paths.  They are not rebuilt,
    guessed at, or normalized by this function.
    """
    database_path = Path(path)
    check = check_database(database_path)
    if check.status != "repairable":
        return RecoveryReport(
            action="repair",
            path=check.path,
            status="owner_action_needed",
            integrity=check.integrity,
            schema=check.schema,
            owner_action_needed=True,
            detail=check.detail,
        )

    backup: Path | None = None
    try:
        backup = _backup_database(database_path)
        db = Database(database_path)
        try:
            # Database() only creates reviewed missing support tables and adds the
            # four reviewed legacy columns after the exact preflight above.
            db.connection.execute(
                "INSERT INTO audit_events (event_type, detail) VALUES (?, ?)",
                ("database_recovery_applied", "known_legacy_migration"),
            )
            db.connection.commit()
        finally:
            db.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        return RecoveryReport(
            action="repair",
            path=str(database_path),
            status="owner_action_needed",
            integrity="failed",
            schema="known_legacy_migration",
            owner_action_needed=True,
            detail=f"repair stopped after backup: {exc}",
            backup_path=str(backup) if backup is not None else None,
        )

    return RecoveryReport(
        action="repair",
        path=str(database_path),
        status="repaired",
        integrity="ok",
        schema="current",
        owner_action_needed=False,
        detail="backup created and known legacy migration applied",
        backup_path=str(backup),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run offline checks/repairs; JSON stdout is the operational audit output."""
    parser = argparse.ArgumentParser(description="Bounded CHC Rental SQLite recovery")
    parser.add_argument("action", choices=("check", "repair"))
    parser.add_argument("database", type=Path, help="explicit local SQLite database path")
    args = parser.parse_args(argv)
    report = check_database(args.database) if args.action == "check" else repair_database(args.database)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.status in {"ok", "repairable", "repaired"} else 2


if __name__ == "__main__":
    sys.exit(main())
