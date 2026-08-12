"""Private SQLite operational ledger for incremental alerts.

This is deliberately not the property's source of truth and not a searchable
property database. YAML remains the owner-controlled configuration authority.
The ledger exists to make source-run recovery, first-observation baselines and
Telegram notification intent transactional.

Legacy daily commands do not import or instantiate this module through a live
connection. The file is created only by the explicit alert migration command.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


LATEST_ALERT_SCHEMA_VERSION = 1


class AlertStoreError(RuntimeError):
    """The operational ledger could not be opened, migrated or verified."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class AlertMigrationStatus:
    exists: bool
    current_version: int
    target_version: int
    pending_versions: tuple[int, ...]
    integrity: str

    @property
    def ready(self) -> bool:
        return (
            self.exists
            and self.current_version == self.target_version
            and not self.pending_versions
            and self.integrity == "ok"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "current_version": self.current_version,
            "target_version": self.target_version,
            "pending_versions": list(self.pending_versions),
            "integrity": self.integrity,
            "ready": self.ready,
        }


class EventStore:
    """Migration and transaction boundary for ``state/alerts.sqlite3``."""

    def __init__(
        self,
        path: str | Path,
        *,
        backup_dir: str | Path,
        migrations_dir: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir)
        self.migrations_dir = (
            Path(migrations_dir)
            if migrations_dir is not None
            else Path(__file__).with_name("migrations")
        )

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.path.resolve()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if not read_only:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _available_migrations(self) -> dict[int, Path]:
        migrations: dict[int, Path] = {}
        if not self.migrations_dir.exists():
            raise AlertStoreError(f"missing alert migrations directory: {self.migrations_dir}")
        for path in sorted(self.migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            try:
                version = int(path.name.split("_", 1)[0])
            except ValueError as exc:
                raise AlertStoreError(f"invalid migration filename: {path.name}") from exc
            if version in migrations:
                raise AlertStoreError(f"duplicate alert migration version: {version}")
            migrations[version] = path
        expected = set(range(1, LATEST_ALERT_SCHEMA_VERSION + 1))
        if set(migrations) != expected:
            raise AlertStoreError(
                f"alert migrations must be contiguous 1..{LATEST_ALERT_SCHEMA_VERSION}; "
                f"found {sorted(migrations)}"
            )
        return migrations

    @staticmethod
    def _current_version(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if row is None:
            return 0
        value = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        return int(value)

    @staticmethod
    def _integrity(connection: sqlite3.Connection) -> str:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        return "; ".join(str(row[0]) for row in rows) if rows else "unknown"

    def migration_status(self) -> AlertMigrationStatus:
        migrations = self._available_migrations()
        if not self.path.exists():
            return AlertMigrationStatus(
                exists=False,
                current_version=0,
                target_version=LATEST_ALERT_SCHEMA_VERSION,
                pending_versions=tuple(sorted(migrations)),
                integrity="missing",
            )
        try:
            with self._connect(read_only=True) as connection:
                current = self._current_version(connection)
                integrity = self._integrity(connection)
        except sqlite3.Error as exc:
            raise AlertStoreError(f"cannot inspect alert ledger {self.path}: {exc}") from exc
        return AlertMigrationStatus(
            exists=True,
            current_version=current,
            target_version=LATEST_ALERT_SCHEMA_VERSION,
            pending_versions=tuple(version for version in migrations if version > current),
            integrity=integrity,
        )

    def migrate(self) -> AlertMigrationStatus:
        """Apply pending migrations atomically, restoring the prior file on error."""
        migrations = self._available_migrations()
        initial = self.migration_status()
        if initial.ready:
            return initial
        existed = self.path.exists()
        backup: Path | None = None
        if existed:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            backup = self.backup_dir / f"alerts-{_utc_stamp()}.sqlite3"
            try:
                with self._connect(read_only=True) as source:
                    with sqlite3.connect(backup) as target:
                        source.backup(target)
            except sqlite3.Error as exc:
                raise AlertStoreError(f"cannot back up alert ledger before migration: {exc}") from exc
            os.chmod(backup, 0o600)

        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            current = self._current_version(connection)
            if current > LATEST_ALERT_SCHEMA_VERSION:
                raise AlertStoreError(
                    f"alert ledger version {current} is newer than supported "
                    f"version {LATEST_ALERT_SCHEMA_VERSION}"
                )
            for version, path in migrations.items():
                if version <= current:
                    continue
                connection.executescript(path.read_text(encoding="utf-8"))
                current = self._current_version(connection)
                if current != version:
                    raise AlertStoreError(
                        f"migration {path.name} did not record schema version {version}"
                    )
            integrity = self._integrity(connection)
            if integrity != "ok":
                raise AlertStoreError(f"alert ledger integrity check failed: {integrity}")
            connection.close()
            connection = None
            os.chmod(self.path, 0o600)
            return self.migration_status()
        except (OSError, sqlite3.Error, AlertStoreError) as exc:
            if connection is not None:
                connection.close()
            for suffix in ("-wal", "-shm"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)
            if backup is not None:
                shutil.copyfile(backup, self.path)
                os.chmod(self.path, 0o600)
            elif not existed:
                self.path.unlink(missing_ok=True)
            if isinstance(exc, AlertStoreError):
                raise
            raise AlertStoreError(f"failed to migrate alert ledger {self.path}: {exc}") from exc

    def table_names(self) -> tuple[str, ...]:
        """Read-only diagnostic used by migration verification and tests."""
        if not self.path.exists():
            return ()
        try:
            with self._connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
        except sqlite3.Error as exc:
            raise AlertStoreError(f"cannot list alert ledger tables: {exc}") from exc
        return tuple(str(row[0]) for row in rows)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Internal-use connection factory for later repository methods.

        Kept as an iterator-shaped helper rather than exposing a connection on
        ``Store``; incremental modules will receive typed EventStore methods as
        they are added, not reach through this boundary.
        """
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()
