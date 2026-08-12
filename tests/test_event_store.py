"""The incremental operational ledger is explicit, transactional and lazy."""

from __future__ import annotations

import sqlite3

import pytest

from chc_rental.event_store import AlertStoreError, EventStore
from chc_rental.store import Store


EXPECTED_TABLES = {
    "delivery_receipts",
    "listing_identities",
    "listing_events",
    "listing_versions",
    "observations",
    "outbox",
    "query_scopes",
    "schema_migrations",
    "source_runs",
}


def test_legacy_store_initialization_does_not_create_sqlite(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    assert store.alert_db_path.exists() is False
    status = store.alert_migration_status()
    assert status.exists is False and status.pending_versions == (1, 2, 3)
    assert store.alert_db_path.exists() is False


def test_explicit_migration_creates_the_complete_owner_only_ledger(store):
    status = store.migrate_alert_ledger()
    assert status.ready is True and status.integrity == "ok"
    assert set(store.event_store().table_names()) == EXPECTED_TABLES
    assert oct(store.alert_db_path.stat().st_mode)[-3:] == "600"

    with sqlite3.connect(store.alert_db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        # Foreign keys are connection-local; EventStore enables them on every
        # managed connection rather than mutating unrelated diagnostic clients.
    with store.event_store().connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migration_is_idempotent_and_does_not_create_spurious_backups(store):
    first = store.migrate_alert_ledger()
    assert first.ready
    backups_before = list(store.backup_dir.glob("alerts-*.sqlite3"))
    second = store.migrate_alert_ledger()
    assert second.ready
    assert list(store.backup_dir.glob("alerts-*.sqlite3")) == backups_before


def test_existing_unversioned_database_is_backed_up_before_migration(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    with sqlite3.connect(store.alert_db_path) as connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT)")
    status = store.migrate_alert_ledger()
    assert status.ready
    backups = list(store.backup_dir.glob("alerts-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='legacy_marker'"
        ).fetchone()


def test_failed_first_migration_removes_the_partial_database(tmp_path):
    migrations = tmp_path / "bad-migrations"
    migrations.mkdir()
    (migrations / "0001_bad.sql").write_text(
        "BEGIN IMMEDIATE; CREATE TABLE partial(x); THIS IS NOT SQL; COMMIT;",
        encoding="utf-8",
    )
    (migrations / "0002_never_reached.sql").write_text(
        "BEGIN IMMEDIATE; COMMIT;", encoding="utf-8"
    )
    (migrations / "0003_never_reached.sql").write_text(
        "BEGIN IMMEDIATE; COMMIT;", encoding="utf-8"
    )
    database = tmp_path / "state" / "alerts.sqlite3"
    event_store = EventStore(
        database,
        backup_dir=tmp_path / "backups",
        migrations_dir=migrations,
    )
    with pytest.raises(AlertStoreError):
        event_store.migrate()
    assert database.exists() is False
    assert (tmp_path / "state" / "alerts.sqlite3-wal").exists() is False
    assert (tmp_path / "state" / "alerts.sqlite3-shm").exists() is False


def test_migration_status_detects_a_database_newer_than_the_application(store):
    store.migrate_alert_ledger()
    with sqlite3.connect(store.alert_db_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (99, 'future', 'now')"
        )
    status = store.alert_migration_status()
    assert status.current_version == 99 and status.ready is False
    with pytest.raises(AlertStoreError, match="newer than supported"):
        store.migrate_alert_ledger()
