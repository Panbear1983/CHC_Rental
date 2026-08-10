"""Migration-safe handling for existing CHC databases created before the
per-profile delivery_time/timezone columns existed.
"""

import sqlite3

import pytest

from chc_rental.db import Database
from chc_rental.repositories import AllowlistRepository, AuditRepository, ProfileRepository

LEGACY_SCHEMA = """
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


@pytest.fixture
def legacy_db_path(tmp_path):
    path = tmp_path / "legacy_chc_rental.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO allowlisted_users (telegram_user_id, display_name) VALUES (111, 'Peter')"
    )
    conn.execute(
        """
        INSERT INTO preference_profiles (
            telegram_user_id, profile_name, city, district,
            price_min, price_max, property_types,
            bed_min, bed_max, bath_min, bath_max,
            required_features, excluded_features, daily_cap, active
        ) VALUES (111, 'Downtown', 'Austin', NULL, 1000, 2000, '["apartment"]', 1, 2, 1.0, 2.0, '[]', '[]', 5, 1)
        """
    )
    conn.commit()
    conn.close()
    return path


def test_opening_legacy_db_adds_missing_columns(legacy_db_path):
    db = Database(legacy_db_path)
    try:
        columns = {row[1] for row in db.connection.execute("PRAGMA table_info(preference_profiles)")}
        assert "delivery_time" in columns
        assert "timezone" in columns
        assert "sqft_min" in columns
        assert "sqft_max" in columns
    finally:
        db.close()


def test_legacy_rows_get_default_delivery_fields(legacy_db_path):
    db = Database(legacy_db_path)
    try:
        allowlist = AllowlistRepository(db)
        audit = AuditRepository(db)
        profiles = ProfileRepository(db, allowlist, audit)

        listed = profiles.list_profiles(111)
        assert len(listed) == 1
        assert listed[0].delivery_time == "09:00"
        assert listed[0].timezone == "America/New_York"
        assert listed[0].sqft_min is None
        assert listed[0].sqft_max is None
    finally:
        db.close()


def test_opening_legacy_db_is_idempotent(legacy_db_path):
    db1 = Database(legacy_db_path)
    db1.close()
    db2 = Database(legacy_db_path)
    try:
        columns = {row[1] for row in db2.connection.execute("PRAGMA table_info(preference_profiles)")}
        assert "delivery_time" in columns
        assert "timezone" in columns
        assert "sqft_min" in columns
        assert "sqft_max" in columns
    finally:
        db2.close()


def test_fresh_database_already_has_delivery_columns():
    db = Database(":memory:")
    try:
        columns = {row[1] for row in db.connection.execute("PRAGMA table_info(preference_profiles)")}
        assert "delivery_time" in columns
        assert "timezone" in columns
        assert "sqft_min" in columns
        assert "sqft_max" in columns
    finally:
        db.close()
