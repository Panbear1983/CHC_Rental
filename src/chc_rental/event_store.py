"""Private SQLite operational ledger for incremental alerts.

This is deliberately not the property's source of truth and not a searchable
property database. YAML remains the owner-controlled configuration authority.
The ledger exists to make source-run recovery, first-observation baselines and
Telegram notification intent transactional.

Legacy daily commands do not import or instantiate this module through a live
connection. The file is created only by the explicit alert migration command.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


LATEST_ALERT_SCHEMA_VERSION = 3


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


@dataclass(frozen=True)
class QueryScopeRecord:
    query_id: str
    source: str
    query_json: str
    baseline_state: str
    next_due_at: str | None
    lease_owner: str | None
    lease_until: str | None


@dataclass(frozen=True)
class SourceRunRecord:
    run_id: str
    query_id: str
    source: str
    apify_run_id: str | None
    default_dataset_id: str | None
    status: str
    input_fingerprint: str
    started_at: str
    finished_at: str | None
    result_count: int | None
    truncated: bool
    error_class: str | None
    error_message: str | None
    charge_usd: float | None
    charge_known: bool
    processed_at: str | None


@dataclass(frozen=True)
class ObservationInput:
    identity_key: str
    version_hash: str
    canonical_json: str
    source: str
    source_listing_id: str | None
    source_url: str
    source_posted_at: str | None


@dataclass(frozen=True)
class ObservationOutcome:
    identity_key: str
    version_hash: str
    baseline: bool
    new_to_scope: bool
    new_identity: bool
    new_version: bool
    previous_canonical_json: str | None
    event_type: str


@dataclass(frozen=True)
class OutboxRecord:
    outbox_id: int
    idempotency_key: str
    telegram_id: int
    search_id: str
    primary_search_name: str
    matching_search_ids: tuple[str, ...]
    identity_key: str
    version_hash: str
    event_type: str
    status: str
    message_text: str
    not_before: str
    attempts: int
    last_error: str | None
    created_at: str
    sent_at: str | None


@dataclass(frozen=True)
class OutboxEnqueueResult:
    outcome: str
    record: OutboxRecord | None


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

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("alert ledger timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _query_scope(row: sqlite3.Row) -> QueryScopeRecord:
        return QueryScopeRecord(
            query_id=str(row["query_id"]),
            source=str(row["source"]),
            query_json=str(row["query_json"]),
            baseline_state=str(row["baseline_state"]),
            next_due_at=row["next_due_at"],
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
        )

    @staticmethod
    def _source_run(row: sqlite3.Row) -> SourceRunRecord:
        return SourceRunRecord(
            run_id=str(row["run_id"]),
            query_id=str(row["query_id"]),
            source=str(row["source"]),
            apify_run_id=row["apify_run_id"],
            default_dataset_id=row["default_dataset_id"],
            status=str(row["status"]),
            input_fingerprint=str(row["input_fingerprint"]),
            started_at=str(row["started_at"]),
            finished_at=row["finished_at"],
            result_count=row["result_count"],
            truncated=bool(row["truncated"]),
            error_class=row["error_class"],
            error_message=row["error_message"],
            charge_usd=row["charge_usd"],
            charge_known=bool(row["charge_known"]),
            processed_at=row["processed_at"],
        )

    def sync_query_scopes(
        self,
        scopes: list[dict[str, str]],
        *,
        now_utc: datetime,
    ) -> None:
        """Make the planned scope set authoritative without resetting cadence."""
        now = self._iso(now_utc)
        active_ids = {scope["query_id"] for scope in scopes}
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if active_ids:
                    placeholders = ",".join("?" for _ in active_ids)
                    connection.execute(
                        f"UPDATE query_scopes SET active=0, retired_at=?, updated_at=? "
                        f"WHERE query_id NOT IN ({placeholders}) AND active=1",
                        (now, now, *sorted(active_ids)),
                    )
                else:
                    connection.execute(
                        "UPDATE query_scopes SET active=0, retired_at=?, updated_at=? WHERE active=1",
                        (now, now),
                    )
                for scope in scopes:
                    connection.execute(
                        """INSERT INTO query_scopes(
                               query_id, source, query_json, next_due_at,
                               created_at, updated_at, active
                           ) VALUES (?, ?, ?, ?, ?, ?, 1)
                           ON CONFLICT(query_id) DO UPDATE SET
                               source=excluded.source,
                               query_json=excluded.query_json,
                               active=1,
                               retired_at=NULL,
                               updated_at=excluded.updated_at""",
                        (
                            scope["query_id"],
                            scope["source"],
                            scope["query_json"],
                            now,
                            now,
                            now,
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def due_query_scopes(self, *, now_utc: datetime, limit: int | None = None) -> list[QueryScopeRecord]:
        now = self._iso(now_utc)
        sql = (
            "SELECT * FROM query_scopes WHERE active=1 "
            "AND (next_due_at IS NULL OR next_due_at<=?) "
            "AND (lease_until IS NULL OR lease_until<?) "
            "ORDER BY COALESCE(next_due_at, created_at), query_id"
        )
        params: list[Any] = [now, now]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._query_scope(row) for row in rows]

    def claim_query_scope(
        self,
        query_id: str,
        *,
        owner: str,
        now_utc: datetime,
        lease_minutes: int,
    ) -> bool:
        now = self._iso(now_utc)
        lease_until = self._iso(now_utc + timedelta(minutes=lease_minutes))
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE query_scopes SET lease_owner=?, lease_until=?, updated_at=?
                       , last_due_at=?
                       WHERE query_id=? AND active=1
                       AND (next_due_at IS NULL OR next_due_at<=?)
                       AND (lease_until IS NULL OR lease_until<?)""",
                    (owner, lease_until, now, now, query_id, now, now),
                )
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    def defer_query_scope(
        self,
        query_id: str,
        *,
        now_utc: datetime,
        next_due_at: datetime,
    ) -> None:
        """Release a lease without counting budget/window deferral as failure."""
        now = self._iso(now_utc)
        next_due = self._iso(next_due_at)
        with self.connection() as connection:
            connection.execute(
                """UPDATE query_scopes SET lease_owner=NULL, lease_until=NULL,
                       next_due_at=?, updated_at=? WHERE query_id=?""",
                (next_due, now, query_id),
            )
            connection.commit()

    def release_query_scope(
        self,
        query_id: str,
        *,
        now_utc: datetime,
        next_due_at: datetime,
        succeeded: bool,
    ) -> None:
        now = self._iso(now_utc)
        next_due = self._iso(next_due_at)
        if succeeded:
            sql = """UPDATE query_scopes SET
                         lease_owner=NULL, lease_until=NULL, last_success_at=?,
                         next_due_at=?, consecutive_failures=0, updated_at=?
                     WHERE query_id=?"""
            params = (now, next_due, now, query_id)
        else:
            sql = """UPDATE query_scopes SET
                         lease_owner=NULL, lease_until=NULL, next_due_at=?,
                         consecutive_failures=consecutive_failures+1, updated_at=?
                     WHERE query_id=?"""
            params = (next_due, now, query_id)
        with self.connection() as connection:
            connection.execute(sql, params)
            connection.commit()

    def create_source_run(
        self,
        *,
        run_id: str,
        query_id: str,
        source: str,
        input_fingerprint: str,
        now_utc: datetime,
    ) -> SourceRunRecord:
        now = self._iso(now_utc)
        try:
            with self.connection() as connection:
                connection.execute(
                    """INSERT INTO source_runs(
                           run_id, query_id, source, status, input_fingerprint,
                           started_at, created_at, updated_at
                       ) VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?)""",
                    (run_id, query_id, source, input_fingerprint, now, now, now),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise AlertStoreError(f"cannot create source run for {query_id}: {exc}") from exc
        found = self.source_run(run_id)
        if found is None:  # pragma: no cover - defensive after committed INSERT
            raise AlertStoreError(f"source run disappeared after insert: {run_id}")
        return found

    def source_run(self, run_id: str) -> SourceRunRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM source_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._source_run(row) if row is not None else None

    def open_source_runs(self) -> list[SourceRunRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM source_runs WHERE status IN ('reserved', 'running') "
                "ORDER BY started_at, run_id"
            ).fetchall()
        return [self._source_run(row) for row in rows]

    def unprocessed_source_runs(self) -> list[SourceRunRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM source_runs
                   WHERE status='succeeded' AND processed_at IS NULL
                   ORDER BY finished_at, run_id"""
            ).fetchall()
        return [self._source_run(row) for row in rows]

    def attach_apify_run(
        self,
        run_id: str,
        *,
        apify_run_id: str,
        default_dataset_id: str | None,
        now_utc: datetime,
    ) -> None:
        now = self._iso(now_utc)
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE source_runs SET apify_run_id=?, default_dataset_id=?,
                       status='running', updated_at=?
                   WHERE run_id=? AND status='reserved'""",
                (apify_run_id, default_dataset_id, now, run_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise AlertStoreError(f"source run is not attachable: {run_id}")

    def finish_source_run(
        self,
        run_id: str,
        *,
        status: str,
        now_utc: datetime,
        result_count: int | None = None,
        truncated: bool = False,
        error_class: str | None = None,
        error_message: str | None = None,
        charge_usd: float | None = None,
        charge_known: bool = False,
        default_dataset_id: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "timed_out", "cancelled"}:
            raise ValueError(f"invalid terminal source-run status: {status}")
        now = self._iso(now_utc)
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE source_runs SET status=?, finished_at=?, result_count=?,
                       truncated=?, error_class=?, error_message=?, charge_usd=?,
                       charge_known=?, default_dataset_id=COALESCE(?, default_dataset_id),
                       updated_at=?
                   WHERE run_id=? AND status IN ('reserved', 'running')""",
                (
                    status,
                    now,
                    result_count,
                    int(truncated),
                    error_class,
                    error_message,
                    charge_usd,
                    int(charge_known),
                    default_dataset_id,
                    now,
                    run_id,
                ),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise AlertStoreError(f"source run is not finishable: {run_id}")

    def query_baseline_states(self) -> dict[str, str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT query_id, baseline_state FROM query_scopes"
            ).fetchall()
        return {str(row["query_id"]): str(row["baseline_state"]) for row in rows}

    def record_observations(
        self,
        *,
        run_id: str,
        items: list[ObservationInput],
        now_utc: datetime,
    ) -> list[ObservationOutcome]:
        """Atomically persist one successful run and establish its baseline."""
        now = self._iso(now_utc)
        outcomes: list[ObservationOutcome] = []
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = connection.execute(
                    "SELECT query_id, source, status FROM source_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise AlertStoreError(f"cannot observe unknown source run: {run_id}")
                if run["status"] != "succeeded":
                    raise AlertStoreError(
                        f"cannot observe non-successful source run {run_id}: {run['status']}"
                    )
                query_id = str(run["query_id"])
                scope = connection.execute(
                    "SELECT baseline_state FROM query_scopes WHERE query_id=?",
                    (query_id,),
                ).fetchone()
                if scope is None:
                    raise AlertStoreError(f"source run has no query scope: {query_id}")
                is_baseline = scope["baseline_state"] != "established"

                for item in items:
                    persisted_event = connection.execute(
                        """SELECT event_type FROM listing_events
                           WHERE run_id=? AND identity_key=? AND version_hash=?""",
                        (run_id, item.identity_key, item.version_hash),
                    ).fetchone()
                    if persisted_event is not None:
                        outcomes.append(
                            ObservationOutcome(
                                identity_key=item.identity_key,
                                version_hash=item.version_hash,
                                baseline=persisted_event["event_type"] == "baseline",
                                new_to_scope=persisted_event["event_type"] == "new",
                                new_identity=False,
                                new_version=False,
                                previous_canonical_json=None,
                                event_type=str(persisted_event["event_type"]),
                            )
                        )
                        continue
                    scope_seen = connection.execute(
                        """SELECT 1 FROM observations o
                           JOIN source_runs r ON r.run_id=o.run_id
                           WHERE r.query_id=? AND o.identity_key=? LIMIT 1""",
                        (query_id, item.identity_key),
                    ).fetchone() is not None
                    identity_exists = connection.execute(
                        "SELECT 1 FROM listing_identities WHERE identity_key=?",
                        (item.identity_key,),
                    ).fetchone() is not None
                    version_exists = connection.execute(
                        """SELECT 1 FROM listing_versions
                           WHERE identity_key=? AND version_hash=?""",
                        (item.identity_key, item.version_hash),
                    ).fetchone() is not None
                    scope_version_seen = connection.execute(
                        """SELECT 1 FROM observations o
                           JOIN source_runs r ON r.run_id=o.run_id
                           WHERE r.query_id=? AND o.identity_key=?
                           AND o.version_hash=? LIMIT 1""",
                        (query_id, item.identity_key, item.version_hash),
                    ).fetchone() is not None
                    previous = connection.execute(
                        """SELECT lv.canonical_json FROM observations o
                           JOIN source_runs r ON r.run_id=o.run_id
                           JOIN listing_versions lv
                             ON lv.identity_key=o.identity_key
                            AND lv.version_hash=o.version_hash
                           WHERE r.query_id=? AND o.identity_key=?
                           ORDER BY o.observed_at DESC, r.started_at DESC LIMIT 1""",
                        (query_id, item.identity_key),
                    ).fetchone()
                    previous_json = str(previous[0]) if previous is not None else None

                    connection.execute(
                        """INSERT INTO listing_identities(
                               identity_key, first_observed_at, last_observed_at
                           ) VALUES (?, ?, ?)
                           ON CONFLICT(identity_key) DO UPDATE SET
                               last_observed_at=excluded.last_observed_at""",
                        (item.identity_key, now, now),
                    )
                    connection.execute(
                        """INSERT INTO listing_versions(
                               identity_key, version_hash, canonical_json,
                               source_posted_at, first_observed_at, last_observed_at
                           ) VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(identity_key, version_hash) DO UPDATE SET
                               canonical_json=excluded.canonical_json,
                               last_observed_at=excluded.last_observed_at""",
                        (
                            item.identity_key,
                            item.version_hash,
                            item.canonical_json,
                            item.source_posted_at,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO observations(
                               run_id, identity_key, version_hash, source,
                               source_listing_id, source_url, observed_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            run_id,
                            item.identity_key,
                            item.version_hash,
                            item.source,
                            item.source_listing_id,
                            item.source_url,
                            now,
                        ),
                    )
                    if is_baseline:
                        event_type = "baseline"
                    elif not scope_seen:
                        event_type = "new"
                    elif not scope_version_seen:
                        previous_price = None
                        if previous_json:
                            try:
                                previous_price = json.loads(previous_json).get("price")
                            except (AttributeError, TypeError, ValueError):
                                previous_price = None
                        try:
                            current_price = json.loads(item.canonical_json).get("price")
                        except (AttributeError, TypeError, ValueError):
                            current_price = None
                        event_type = (
                            "price_change"
                            if previous_price != current_price
                            else "material_change"
                        )
                    else:
                        event_type = "duplicate"
                    connection.execute(
                        """INSERT INTO listing_events(
                               run_id, query_id, identity_key, version_hash,
                               event_type, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            run_id,
                            query_id,
                            item.identity_key,
                            item.version_hash,
                            event_type,
                            now,
                        ),
                    )
                    outcomes.append(
                        ObservationOutcome(
                            identity_key=item.identity_key,
                            version_hash=item.version_hash,
                            baseline=is_baseline,
                            new_to_scope=not scope_seen,
                            new_identity=not identity_exists,
                            new_version=not version_exists,
                            previous_canonical_json=previous_json,
                            event_type=event_type,
                        )
                    )

                if is_baseline:
                    connection.execute(
                        """UPDATE query_scopes SET baseline_state='established',
                               baseline_established_at=?, updated_at=?
                           WHERE query_id=?""",
                        (now, now, query_id),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return outcomes

    def mark_source_run_processed(self, run_id: str, *, now_utc: datetime) -> None:
        now = self._iso(now_utc)
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE source_runs SET processed_at=?, updated_at=?
                   WHERE run_id=? AND status='succeeded' AND processed_at IS NULL""",
                (now, now, run_id),
            )
            connection.commit()
        if cursor.rowcount not in (0, 1):  # pragma: no cover - PK makes >1 impossible
            raise AlertStoreError(f"unexpected processed row count for {run_id}")

    @staticmethod
    def _outbox_record(row: sqlite3.Row) -> OutboxRecord:
        return OutboxRecord(
            outbox_id=int(row["outbox_id"]),
            idempotency_key=str(row["idempotency_key"]),
            telegram_id=int(row["telegram_id"]),
            search_id=str(row["search_id"]),
            primary_search_name=str(row["primary_search_name"]),
            matching_search_ids=tuple(json.loads(row["matching_search_ids_json"])),
            identity_key=str(row["identity_key"]),
            version_hash=str(row["version_hash"]),
            event_type=str(row["event_type"]),
            status=str(row["status"]),
            message_text=str(row["message_text"]),
            not_before=str(row["not_before"]),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            created_at=str(row["created_at"]),
            sent_at=row["sent_at"],
        )

    def enqueue_shadow(
        self,
        *,
        idempotency_key: str,
        telegram_id: int,
        search_id: str,
        primary_search_name: str,
        matching_search_ids: list[str],
        identity_key: str,
        version_hash: str,
        event_type: str,
        message_text: str,
        not_before_utc: datetime,
        created_at_utc: datetime,
        cap_window_start_utc: datetime,
        cap_window_end_utc: datetime,
        daily_cap: int,
    ) -> OutboxEnqueueResult:
        """Reserve one search-cap slot and insert idempotent shadow intent."""
        created = self._iso(created_at_utc)
        not_before = self._iso(not_before_utc)
        window_start = self._iso(cap_window_start_utc)
        window_end = self._iso(cap_window_end_utc)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM outbox WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return OutboxEnqueueResult(
                        "duplicate", self._outbox_record(existing)
                    )
                used = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM outbox
                           WHERE search_id=? AND created_at>=? AND created_at<?
                           AND status!='cancelled'""",
                        (search_id, window_start, window_end),
                    ).fetchone()[0]
                )
                if used >= daily_cap:
                    connection.commit()
                    return OutboxEnqueueResult("cap_reached", None)
                cursor = connection.execute(
                    """INSERT INTO outbox(
                           idempotency_key, telegram_id, search_id,
                           primary_search_name, matching_search_ids_json,
                           identity_key, version_hash, event_type, message_text,
                           not_before, status, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'shadow', ?)""",
                    (
                        idempotency_key,
                        telegram_id,
                        search_id,
                        primary_search_name,
                        json.dumps(matching_search_ids, separators=(",", ":")),
                        identity_key,
                        version_hash,
                        event_type,
                        message_text,
                        not_before,
                        created,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM outbox WHERE outbox_id=?", (cursor.lastrowid,)
                ).fetchone()
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                with self.connection() as reader:
                    row = reader.execute(
                        "SELECT * FROM outbox WHERE idempotency_key=?",
                        (idempotency_key,),
                    ).fetchone()
                if row is not None:
                    return OutboxEnqueueResult("duplicate", self._outbox_record(row))
                raise
            except BaseException:
                connection.rollback()
                raise
        if row is None:  # pragma: no cover - defensive after INSERT
            raise AlertStoreError("outbox row disappeared after insert")
        return OutboxEnqueueResult("queued", self._outbox_record(row))

    def outbox_records(self, *, status: str | None = None) -> list[OutboxRecord]:
        sql = "SELECT * FROM outbox"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY outbox_id"
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._outbox_record(row) for row in rows]

    def outbox_counts(self) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM outbox GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

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
