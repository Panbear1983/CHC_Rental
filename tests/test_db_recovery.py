"""Bounded, offline SQLite health and recovery behavior."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from chc_rental.db import Database
import chc_rental.db_recovery as recovery


def test_healthy_database_reports_ok_without_mutating_it(tmp_path: Path) -> None:
    path = tmp_path / "healthy.sqlite3"
    db = Database(path)
    db.close()
    before = path.read_bytes()

    report = recovery.check_database(path)

    assert report.status == "ok"
    assert report.integrity == "ok"
    assert report.schema == "current"
    assert report.owner_action_needed is False
    assert path.read_bytes() == before


def test_repair_known_legacy_migration_creates_backup_and_audit_event(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _create_legacy_database(path)

    report = recovery.check_database(path)
    assert report.status == "repairable"
    assert report.schema == "known_legacy_migration"

    repaired = recovery.repair_database(path)

    assert repaired.status == "repaired"
    assert repaired.backup_path is not None
    backup = Path(repaired.backup_path)
    assert backup.exists()
    assert _columns(backup, "preference_profiles").isdisjoint(
        {"delivery_time", "timezone", "sqft_min", "sqft_max"}
    )
    assert _columns(path, "preference_profiles") >= {
        "delivery_time", "timezone", "sqft_min", "sqft_max"
    }
    conn = sqlite3.connect(path)
    try:
        audit = conn.execute(
            "SELECT event_type, detail FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert audit == ("database_recovery_applied", "known_legacy_migration")


def test_malformed_database_stops_with_owner_action_needed_without_backup_or_mutation(tmp_path: Path) -> None:
    path = tmp_path / "malformed.sqlite3"
    path.write_bytes(b"not a sqlite database")
    before = path.read_bytes()

    report = recovery.check_database(path)
    repaired = recovery.repair_database(path)

    assert report.status == "owner_action_needed"
    assert report.integrity == "unreadable"
    assert repaired.status == "owner_action_needed"
    assert repaired.backup_path is None
    assert path.read_bytes() == before
    assert list(tmp_path.glob("malformed.sqlite3.*.bak")) == []


def test_repair_refuses_unknown_schema_without_backup_or_semantic_changes(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.sqlite3"
    db = Database(path)
    db.connection.execute("ALTER TABLE preference_profiles ADD COLUMN unreviewed_field TEXT")
    db.connection.commit()
    db.close()
    before = path.read_bytes()

    repaired = recovery.repair_database(path)

    assert repaired.status == "owner_action_needed"
    assert repaired.schema == "unknown"
    assert repaired.backup_path is None
    assert path.read_bytes() == before
    assert list(tmp_path.glob("unsafe.sqlite3.*.bak")) == []


def test_cli_check_emits_json_audit_output_without_repair(tmp_path: Path, capsys) -> None:
    from chc_rental.db_recovery import main

    path = tmp_path / "cli.sqlite3"
    db = Database(path)
    db.close()

    exit_code = main(["check", str(path)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["action"] == "check"
    assert payload["status"] == "ok"
    assert payload["backup_path"] is None


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _create_legacy_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE allowlisted_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE preference_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                profile_name TEXT NOT NULL,
                city TEXT NOT NULL,
                district TEXT,
                price_min INTEGER NOT NULL,
                price_max INTEGER NOT NULL,
                property_types TEXT NOT NULL,
                bed_min INTEGER NOT NULL,
                bed_max INTEGER NOT NULL,
                bath_min REAL NOT NULL,
                bath_max REAL NOT NULL,
                required_features TEXT NOT NULL,
                excluded_features TEXT NOT NULL,
                daily_cap INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (telegram_user_id, profile_name),
                FOREIGN KEY (telegram_user_id) REFERENCES allowlisted_users (telegram_user_id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
