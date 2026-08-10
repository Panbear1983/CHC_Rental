"""SQLite connection and schema management."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS allowlisted_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS preference_profiles (
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
    sqft_min INTEGER,
    sqft_max INTEGER,
    required_features TEXT NOT NULL,
    excluded_features TEXT NOT NULL,
    daily_cap INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    delivery_time TEXT NOT NULL DEFAULT '09:00',
    timezone TEXT NOT NULL DEFAULT 'America/New_York',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (telegram_user_id, profile_name),
    FOREIGN KEY (telegram_user_id) REFERENCES allowlisted_users (telegram_user_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    telegram_user_id INTEGER,
    profile_id INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS delivery_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    profile_id INTEGER,
    listing_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (telegram_user_id, listing_key)
);

CREATE TABLE IF NOT EXISTS daily_budget_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_date TEXT NOT NULL UNIQUE,
    daily_limit INTEGER NOT NULL,
    allocated INTEGER NOT NULL DEFAULT 0,
    circuit_breaker_tripped INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

_PREFERENCE_PROFILE_COLUMN_MIGRATIONS = (
    ("delivery_time", "TEXT NOT NULL DEFAULT '09:00'"),
    ("timezone", "TEXT NOT NULL DEFAULT 'America/New_York'"),
    ("sqft_min", "INTEGER"),
    ("sqft_max", "INTEGER"),
)


def _migrate_preference_profiles(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database's original creation.

    Existing CHC databases predate the delivery_time/timezone columns; this
    keeps opening them from failing on missing-column errors instead of
    requiring a manual migration step.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(preference_profiles)")}
    for column, ddl in _PREFERENCE_PROFILE_COLUMN_MIGRATIONS:
        if column not in existing:
            conn.execute(f"ALTER TABLE preference_profiles ADD COLUMN {column} {ddl}")
    conn.commit()


class Database:
    """Owns a single SQLite connection and ensures the schema exists."""

    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        _migrate_preference_profiles(self._conn)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()
