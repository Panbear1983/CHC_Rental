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
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


LATEST_ALERT_SCHEMA_VERSION = 7


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
    execution_mode: str
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
class RolloutAttestation:
    gate: str
    evidence: str
    attested_at: str


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


@dataclass(frozen=True)
class SourceBreakerRecord:
    source: str
    state: str
    consecutive_failures: int
    last_error_class: str | None
    last_error_message: str | None
    opened_at: str | None
    retry_at: str | None
    last_success_at: str | None
    pending_alert: str | None
    failure_alerted_at: str | None


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
            execution_mode=str(row["execution_mode"]),
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
        execution_mode: str,
        input_fingerprint: str,
        now_utc: datetime,
    ) -> SourceRunRecord:
        if execution_mode not in {"live", "fixture"}:
            raise ValueError("source run execution mode must be live or fixture")
        now = self._iso(now_utc)
        try:
            with self.connection() as connection:
                connection.execute(
                    """INSERT INTO source_runs(
                           run_id, query_id, source, execution_mode, status, input_fingerprint,
                           started_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?, ?)""",
                    (
                        run_id,
                        query_id,
                        source,
                        execution_mode,
                        input_fingerprint,
                        now,
                        now,
                        now,
                    ),
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

    def retry_failed_outbox(self, outbox_id: int, *, now_utc: datetime) -> None:
        """Move one definitely failed item to retry_wait; uncertain is forbidden."""
        now = self._iso(now_utc)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE outbox SET status='retry_wait', not_before=?,
                           claimed_at=NULL, last_error=NULL, last_error_class=NULL,
                           operator_alerted_at=NULL
                       WHERE outbox_id=? AND status='failed'""",
                    (now, outbox_id),
                )
                if cursor.rowcount != 1:
                    current = connection.execute(
                        "SELECT status FROM outbox WHERE outbox_id=?", (outbox_id,)
                    ).fetchone()
                    state = current["status"] if current is not None else "missing"
                    raise AlertStoreError(
                        f"outbox {outbox_id} is not definitely failed (status {state})"
                    )
                connection.execute(
                    """INSERT INTO operator_audit(
                           action, target_type, target_id, details_json, created_at
                       ) VALUES ('retry_failed_outbox', 'outbox', ?, '{}', ?)""",
                    (str(outbox_id), now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def outbox_listing_json(self, outbox_id: int) -> str:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT lv.canonical_json FROM outbox o
                   JOIN listing_versions lv
                     ON lv.identity_key=o.identity_key
                    AND lv.version_hash=o.version_hash
                   WHERE o.outbox_id=?""",
                (outbox_id,),
            ).fetchone()
        if row is None:
            raise AlertStoreError(f"outbox listing context is missing: {outbox_id}")
        return str(row["canonical_json"])

    def promote_shadow(self, outbox_id: int, *, now_utc: datetime) -> bool:
        now = self._iso(now_utc)
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status='pending', last_error=NULL
                   WHERE outbox_id=? AND status='shadow'""",
                (outbox_id,),
            )
            if cursor.rowcount:
                connection.execute(
                    """INSERT INTO operator_audit(
                           action, target_type, target_id, details_json, created_at
                       ) VALUES ('promote_canary_outbox', 'outbox', ?, '{}', ?)""",
                    (str(outbox_id), now),
                )
            connection.commit()
        return cursor.rowcount == 1

    def cancel_outbox(
        self, outbox_id: int, *, now_utc: datetime, reason: str
    ) -> bool:
        now = self._iso(now_utc)
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status='cancelled', claimed_at=NULL,
                       last_error_class='ineligible', last_error=?
                   WHERE outbox_id=?
                   AND status IN ('shadow', 'pending', 'sending', 'retry_wait', 'failed')""",
                (reason[:500], outbox_id),
            )
            connection.commit()
        return cursor.rowcount == 1

    def cancel_recipient_outbox(
        self,
        telegram_ids: set[int],
        *,
        now_utc: datetime,
        reason: str,
    ) -> int:
        """Cancel every still-actionable row for explicit recipients.

        Completed receipts and uncertain outcomes remain immutable history.
        A missing ledger means incremental alerts were never initialized and is
        therefore a successful no-op rather than a reason to create SQLite.
        """
        if not telegram_ids or not self.path.exists():
            return 0
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in telegram_ids
        ):
            raise ValueError("Telegram IDs must be positive integers")
        ids = sorted(telegram_ids)
        placeholders = ",".join("?" for _ in ids)
        now = self._iso(now_utc)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    f"""UPDATE outbox SET status='cancelled', claimed_at=NULL,
                               last_error_class='ineligible', last_error=?
                           WHERE telegram_id IN ({placeholders})
                           AND status IN (
                               'shadow', 'pending', 'sending', 'retry_wait', 'failed'
                           )""",
                    (reason[:500], *ids),
                )
                cancelled = cursor.rowcount
                has_operator_audit = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='operator_audit'"
                ).fetchone()
                if cancelled and has_operator_audit:
                    connection.execute(
                        """INSERT INTO operator_audit(
                               action, target_type, target_id, details_json, created_at
                           ) VALUES (
                               'cancel_recipient_outbox', 'telegram_recipient', ?, ?, ?
                           )""",
                        (
                            ",".join(map(str, ids)),
                            json.dumps(
                                {"cancelled": cancelled, "reason": reason[:500]},
                                sort_keys=True,
                            ),
                            now,
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return cancelled

    def release_due_retries(
        self, *, now_utc: datetime, telegram_ids: set[int]
    ) -> int:
        if not telegram_ids:
            return 0
        now = self._iso(now_utc)
        placeholders = ",".join("?" for _ in telegram_ids)
        params: tuple[Any, ...] = (now, *sorted(telegram_ids))
        with self.connection() as connection:
            cursor = connection.execute(
                f"""UPDATE outbox SET status='pending'
                    WHERE status='retry_wait' AND not_before<=?
                    AND telegram_id IN ({placeholders})""",
                params,
            )
            connection.commit()
        return cursor.rowcount

    def claim_due_outbox(
        self,
        *,
        now_utc: datetime,
        telegram_ids: set[int],
        outbox_id: int | None = None,
    ) -> OutboxRecord | None:
        if not telegram_ids:
            return None
        now = self._iso(now_utc)
        placeholders = ",".join("?" for _ in telegram_ids)
        params: list[Any] = [now, *sorted(telegram_ids)]
        selected_filter = ""
        if outbox_id is not None:
            selected_filter = " AND outbox_id=?"
            params.append(outbox_id)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                selected = connection.execute(
                    f"""SELECT outbox_id FROM outbox
                        WHERE status='pending' AND not_before<=?
                        AND telegram_id IN ({placeholders})
                        {selected_filter}
                        ORDER BY not_before, outbox_id LIMIT 1""",
                    tuple(params),
                ).fetchone()
                if selected is None:
                    connection.commit()
                    return None
                outbox_id = int(selected["outbox_id"])
                cursor = connection.execute(
                    """UPDATE outbox SET status='sending', attempts=attempts+1,
                           claimed_at=?, last_error=NULL, last_error_class=NULL
                       WHERE outbox_id=? AND status='pending'""",
                    (now, outbox_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                row = connection.execute(
                    "SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)
                ).fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self._outbox_record(row) if row is not None else None

    def mark_outbox_sent(
        self,
        outbox_id: int,
        *,
        now_utc: datetime,
        telegram_message_id: str | None,
    ) -> None:
        now = self._iso(now_utc)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE outbox SET status='sent', sent_at=?, claimed_at=NULL,
                           last_error=NULL, last_error_class=NULL
                       WHERE outbox_id=? AND status='sending'""",
                    (now, outbox_id),
                )
                if cursor.rowcount != 1:
                    raise AlertStoreError(f"outbox {outbox_id} is not sending")
                connection.execute(
                    """INSERT INTO delivery_receipts(
                           outbox_id, telegram_message_id, accepted_at
                       ) VALUES (?, ?, ?)""",
                    (outbox_id, telegram_message_id, now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def mark_outbox_failure(
        self,
        outbox_id: int,
        *,
        now_utc: datetime,
        error_class: str,
        error_message: str,
        retry_at_utc: datetime | None,
        uncertain: bool = False,
    ) -> str:
        now = self._iso(now_utc)
        if uncertain:
            status = "uncertain"
            not_before = now
        elif retry_at_utc is not None:
            status = "retry_wait"
            not_before = self._iso(retry_at_utc)
        else:
            status = "failed"
            not_before = now
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status=?, not_before=?, claimed_at=NULL,
                       last_error_class=?, last_error=?
                   WHERE outbox_id=? AND status='sending'""",
                (status, not_before, error_class[:100], error_message[:500], outbox_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise AlertStoreError(f"outbox {outbox_id} is not sending")
        return status

    def return_outbox_to_pending(
        self,
        outbox_id: int,
        *,
        now_utc: datetime,
        reason: str,
        not_before_utc: datetime | None = None,
    ) -> None:
        now = self._iso(now_utc)
        not_before = self._iso(not_before_utc) if not_before_utc is not None else now
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status='pending', not_before=?, claimed_at=NULL,
                       last_error_class='gate_changed', last_error=?
                   WHERE outbox_id=? AND status='sending'""",
                (not_before, reason[:500], outbox_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise AlertStoreError(f"outbox {outbox_id} is not sending")

    def recover_stale_sending(
        self,
        *,
        now_utc: datetime,
        stale_before_utc: datetime,
        telegram_ids: set[int],
    ) -> int:
        if not telegram_ids:
            return 0
        now = self._iso(now_utc)
        stale_before = self._iso(stale_before_utc)
        placeholders = ",".join("?" for _ in telegram_ids)
        params: tuple[Any, ...] = (
            "worker restarted while Telegram acceptance was unknown",
            now,
            stale_before,
            *sorted(telegram_ids),
        )
        with self.connection() as connection:
            cursor = connection.execute(
                f"""UPDATE outbox SET status='uncertain', claimed_at=NULL,
                           last_error_class='stale_sending', last_error=?, updated_at=?
                       WHERE status='sending' AND claimed_at<?
                       AND telegram_id IN ({placeholders})""",
                params,
            )
            connection.commit()
        return cursor.rowcount

    def terminal_failures_needing_alert(self) -> list[OutboxRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM outbox WHERE status IN ('failed', 'uncertain')
                   AND operator_alerted_at IS NULL ORDER BY outbox_id"""
            ).fetchall()
        return [self._outbox_record(row) for row in rows]

    def mark_terminal_failure_alerted(
        self, outbox_ids: list[int], *, now_utc: datetime
    ) -> None:
        if not outbox_ids:
            return
        now = self._iso(now_utc)
        placeholders = ",".join("?" for _ in outbox_ids)
        with self.connection() as connection:
            connection.execute(
                f"""UPDATE outbox SET operator_alerted_at=?
                    WHERE outbox_id IN ({placeholders})
                    AND status IN ('failed', 'uncertain')""",
                (now, *outbox_ids),
            )
            connection.commit()

    def reset_query_baseline(
        self, query_id: str, *, now_utc: datetime, reason: str
    ) -> None:
        """Make the next successful collection silent and record the action."""
        now = self._iso(now_utc)
        reason_text = reason.strip() or "owner requested from dashboard"
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                in_flight = connection.execute(
                    """SELECT COUNT(*) FROM source_runs
                       WHERE query_id=? AND (
                           status IN ('reserved', 'running') OR
                           (status='succeeded' AND processed_at IS NULL)
                       )""",
                    (query_id,),
                ).fetchone()[0]
                if in_flight:
                    raise AlertStoreError(
                        "cannot reset a baseline while its source run is open or unprocessed"
                    )
                cursor = connection.execute(
                    """UPDATE query_scopes SET baseline_state='reset_pending',
                           baseline_established_at=NULL, updated_at=?
                       WHERE query_id=?""",
                    (now, query_id),
                )
                if cursor.rowcount != 1:
                    raise AlertStoreError(f"unknown query scope: {query_id}")
                connection.execute(
                    """INSERT INTO operator_audit(
                           action, target_type, target_id, details_json, created_at
                       ) VALUES ('reset_baseline', 'query_scope', ?, ?, ?)""",
                    (query_id, json.dumps({"reason": reason_text}), now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def outbox_counts(self, *, telegram_id: int | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS count FROM outbox"
        params: tuple[Any, ...] = ()
        if telegram_id is not None:
            sql += " WHERE telegram_id=?"
            params = (telegram_id,)
        sql += " GROUP BY status"
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def health_snapshot(self) -> dict[str, Any]:
        """Read-only incremental scheduler, source-run, cost, and outbox health."""
        with self.connection() as connection:
            scope = connection.execute(
                """SELECT COUNT(*) AS active_scopes,
                          SUM(CASE WHEN baseline_state!='established' THEN 1 ELSE 0 END)
                              AS pending_baselines,
                          MIN(next_due_at) AS next_due_at
                   FROM query_scopes WHERE active=1"""
            ).fetchone()
            latest = connection.execute(
                "SELECT * FROM source_runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
            success = connection.execute(
                """SELECT finished_at FROM source_runs
                   WHERE status='succeeded'
                   ORDER BY finished_at DESC, run_id DESC LIMIT 1"""
            ).fetchone()
            running = int(
                connection.execute(
                    "SELECT COUNT(*) FROM source_runs WHERE status IN ('reserved', 'running')"
                ).fetchone()[0]
            )
            cost = connection.execute(
                """SELECT COALESCE(SUM(charge_usd), 0) AS known_cost,
                          SUM(CASE WHEN charge_known=0 THEN 1 ELSE 0 END) AS unknown_charges
                   FROM source_runs WHERE execution_mode='live'"""
            ).fetchone()
            query_rows = connection.execute(
                "SELECT * FROM query_scopes WHERE active=1 ORDER BY query_id"
            ).fetchall()
            scopes = []
            for row in query_rows:
                latest_row = connection.execute(
                    """SELECT * FROM source_runs WHERE query_id=?
                       ORDER BY started_at DESC, run_id DESC LIMIT 1""",
                    (row["query_id"],),
                ).fetchone()
                latest_scope_run = (
                    self._source_run(latest_row) if latest_row is not None else None
                )
                scopes.append(
                    {
                        "query_id": str(row["query_id"]),
                        "source": str(row["source"]),
                        "baseline_state": str(row["baseline_state"]),
                        "last_success_at": row["last_success_at"],
                        "next_due_at": row["next_due_at"],
                        "consecutive_failures": int(row["consecutive_failures"]),
                        "run_id": latest_scope_run.run_id if latest_scope_run else None,
                        "apify_run_id": (
                            latest_scope_run.apify_run_id if latest_scope_run else None
                        ),
                        "run_status": (
                            latest_scope_run.status if latest_scope_run else None
                        ),
                        "result_count": (
                            latest_scope_run.result_count if latest_scope_run else None
                        ),
                        "truncated": (
                            latest_scope_run.truncated if latest_scope_run else False
                        ),
                        "error": (
                            latest_scope_run.error_message
                            or latest_scope_run.error_class
                            if latest_scope_run
                            else None
                        ),
                    }
                )
        latest_run = self._source_run(latest) if latest is not None else None
        return {
            "active_scopes": int(scope["active_scopes"] or 0),
            "pending_baselines": int(scope["pending_baselines"] or 0),
            "next_due_at": scope["next_due_at"],
            "running_runs": running,
            "last_attempt_at": latest_run.started_at if latest_run else None,
            "last_status": latest_run.status if latest_run else None,
            "last_result_count": latest_run.result_count if latest_run else None,
            "last_truncated": latest_run.truncated if latest_run else False,
            "last_error": (
                latest_run.error_message or latest_run.error_class if latest_run else None
            ),
            "last_success_at": success["finished_at"] if success is not None else None,
            "known_cost_usd": float(cost["known_cost"] or 0),
            "unknown_charges": int(cost["unknown_charges"] or 0),
            "outbox": self.outbox_counts(),
            "scopes": scopes,
            "breakers": [
                {
                    "source": item.source,
                    "state": item.state,
                    "consecutive_failures": item.consecutive_failures,
                    "last_error_class": item.last_error_class,
                    "last_error_message": item.last_error_message,
                    "opened_at": item.opened_at,
                    "retry_at": item.retry_at,
                    "last_success_at": item.last_success_at,
                    "pending_alert": item.pending_alert,
                }
                for item in self.source_breakers()
            ],
        }

    @staticmethod
    def _breaker(row: sqlite3.Row) -> SourceBreakerRecord:
        return SourceBreakerRecord(
            source=str(row["source"]),
            state=str(row["state"]),
            consecutive_failures=int(row["consecutive_failures"]),
            last_error_class=row["last_error_class"],
            last_error_message=row["last_error_message"],
            opened_at=row["opened_at"],
            retry_at=row["retry_at"],
            last_success_at=row["last_success_at"],
            pending_alert=row["pending_alert"],
            failure_alerted_at=row["failure_alerted_at"],
        )

    def source_breakers(self) -> list[SourceBreakerRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM source_breakers ORDER BY source"
            ).fetchall()
        return [self._breaker(row) for row in rows]

    def source_start_allowed(self, source: str, *, now_utc: datetime) -> bool:
        """Claim one half-open probe after cooldown; closed sources pass freely."""
        now = self._iso(now_utc)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM source_breakers WHERE source=?", (source,)
                ).fetchone()
                if row is None or row["state"] == "closed":
                    connection.commit()
                    return True
                if row["state"] == "half_open" or not row["retry_at"]:
                    connection.commit()
                    return False
                if str(row["retry_at"]) > now:
                    connection.commit()
                    return False
                cursor = connection.execute(
                    """UPDATE source_breakers SET state='half_open', updated_at=?
                       WHERE source=? AND state='open' AND retry_at<=?""",
                    (now, source, now),
                )
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    def release_source_probe(self, source: str, *, now_utc: datetime) -> None:
        """Return an unspent half-open probe when a local budget blocks its start."""
        now = self._iso(now_utc)
        with self.connection() as connection:
            connection.execute(
                """UPDATE source_breakers SET state='open', updated_at=?
                   WHERE source=? AND state='half_open'""",
                (now, source),
            )
            connection.commit()

    def record_source_failure(
        self,
        source: str,
        *,
        now_utc: datetime,
        error_class: str,
        error_message: str | None,
        threshold: int,
        cooldown_minutes: int,
        immediate_open: bool = False,
    ) -> SourceBreakerRecord:
        now = self._iso(now_utc)
        retry_at = self._iso(now_utc + timedelta(minutes=cooldown_minutes))
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM source_breakers WHERE source=?", (source,)
                ).fetchone()
                previous_state = str(row["state"]) if row is not None else "closed"
                previous_class = row["last_error_class"] if row is not None else None
                previous_pending = row["pending_alert"] if row is not None else None
                previous_alerted = row["failure_alerted_at"] if row is not None else None
                failures = int(row["consecutive_failures"] if row is not None else 0) + 1
                should_open = (
                    immediate_open
                    or failures >= threshold
                    or previous_state in {"open", "half_open"}
                )
                state = "open" if should_open else "closed"
                pending = previous_pending
                failure_alerted_at = previous_alerted
                opened_at = row["opened_at"] if row is not None else None
                if failures == 1 and not should_open:
                    pending = "failure_started"
                    failure_alerted_at = None
                elif should_open and previous_state == "closed":
                    pending = "opened"
                    failure_alerted_at = None
                    opened_at = now
                elif (
                    should_open
                    and previous_state in {"open", "half_open"}
                    and previous_class is not None
                    and previous_class != error_class
                ):
                    pending = "escalated"
                    failure_alerted_at = None
                connection.execute(
                    """INSERT INTO source_breakers(
                           source, state, consecutive_failures, last_error_class,
                           last_error_message, opened_at, retry_at, pending_alert,
                           failure_alerted_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source) DO UPDATE SET
                           state=excluded.state,
                           consecutive_failures=excluded.consecutive_failures,
                           last_error_class=excluded.last_error_class,
                           last_error_message=excluded.last_error_message,
                           opened_at=excluded.opened_at,
                           retry_at=excluded.retry_at,
                           pending_alert=excluded.pending_alert,
                           failure_alerted_at=excluded.failure_alerted_at,
                           updated_at=excluded.updated_at""",
                    (
                        source,
                        state,
                        failures,
                        error_class[:100],
                        (error_message or "")[:500] or None,
                        opened_at,
                        retry_at if should_open else None,
                        pending,
                        failure_alerted_at,
                        now,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return next(item for item in self.source_breakers() if item.source == source)

    def record_source_success(
        self, source: str, *, now_utc: datetime
    ) -> SourceBreakerRecord:
        now = self._iso(now_utc)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM source_breakers WHERE source=?", (source,)
                ).fetchone()
                recovered = bool(
                    row is not None
                    and int(row["consecutive_failures"]) > 0
                    and row["failure_alerted_at"] is not None
                )
                connection.execute(
                    """INSERT INTO source_breakers(
                           source, state, consecutive_failures, last_success_at,
                           pending_alert, updated_at
                       ) VALUES (?, 'closed', 0, ?, ?, ?)
                       ON CONFLICT(source) DO UPDATE SET
                           state='closed', consecutive_failures=0,
                           last_error_class=NULL, last_error_message=NULL,
                           opened_at=NULL, retry_at=NULL, last_success_at=excluded.last_success_at,
                           pending_alert=excluded.pending_alert, updated_at=excluded.updated_at""",
                    (source, now, "recovered" if recovered else None, now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return next(item for item in self.source_breakers() if item.source == source)

    def pending_source_alerts(self) -> list[SourceBreakerRecord]:
        return [item for item in self.source_breakers() if item.pending_alert]

    def acknowledge_source_alert(self, source: str, *, now_utc: datetime) -> None:
        now = self._iso(now_utc)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT pending_alert FROM source_breakers WHERE source=?", (source,)
            ).fetchone()
            if row is None or row["pending_alert"] is None:
                return
            failure_alerted = (
                now
                if row["pending_alert"]
                in {"failure_started", "opened", "escalated"}
                else None
            )
            connection.execute(
                """UPDATE source_breakers SET pending_alert=NULL,
                       failure_alerted_at=?, updated_at=? WHERE source=?""",
                (failure_alerted, now, source),
            )
            connection.commit()

    def monthly_source_cost(self, source: str, *, now_utc: datetime) -> dict[str, Any]:
        now = now_utc.astimezone(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT charge_usd, charge_known FROM source_runs
                   WHERE source=? AND execution_mode='live'
                   AND started_at>=? AND started_at<?""",
                (source, self._iso(start), self._iso(end)),
            ).fetchall()
        known_values = sorted(
            float(row["charge_usd"])
            for row in rows
            if row["charge_known"] and row["charge_usd"] is not None
        )
        p95 = None
        if known_values:
            index = max(0, min(len(known_values) - 1, (95 * len(known_values) - 1) // 100))
            p95 = known_values[index]
        return {
            "month": start.strftime("%Y-%m"),
            "known_cost_usd": sum(known_values),
            "known_runs": len(known_values),
            "unknown_runs": sum(not bool(row["charge_known"]) for row in rows),
            "p95_cost_usd": p95,
        }

    def source_rollout_metrics(self, source: str) -> dict[str, Any]:
        """Return retained duration/cost/coverage evidence for rollout gates."""
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT started_at, finished_at, status, result_count, truncated,
                          charge_usd, charge_known
                   FROM source_runs
                   WHERE source=? AND execution_mode='live' ORDER BY started_at""",
                (source,),
            ).fetchall()
        durations: list[float] = []
        known_costs: list[float] = []
        for row in rows:
            if row["finished_at"]:
                started = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
                finished = datetime.fromisoformat(
                    str(row["finished_at"]).replace("Z", "+00:00")
                )
                durations.append(max(0.0, (finished - started).total_seconds()))
            if row["charge_known"] and row["charge_usd"] is not None:
                known_costs.append(float(row["charge_usd"]))

        def p95(values: list[float]) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            index = max(0, min(len(ordered) - 1, (95 * len(ordered) - 1) // 100))
            return ordered[index]

        return {
            "retained_runs": len(rows),
            "successful_runs": sum(row["status"] == "succeeded" for row in rows),
            "failed_runs": sum(
                row["status"] in {"failed", "timed_out", "cancelled"}
                for row in rows
            ),
            "known_cost_runs": len(known_costs),
            "unknown_cost_runs": sum(not bool(row["charge_known"]) for row in rows),
            "p95_cost_usd": p95(known_costs),
            "p95_duration_seconds": p95(durations),
            "truncated_runs": sum(bool(row["truncated"]) for row in rows),
            "last_result_count": rows[-1]["result_count"] if rows else None,
            "last_success_at": next(
                (
                    row["finished_at"]
                    for row in reversed(rows)
                    if row["status"] == "succeeded"
                ),
                None,
            ),
        }

    def record_scheduler_tick(
        self,
        *,
        mode: str,
        status: str,
        source_started: int,
        source_succeeded: int,
        source_failed: int,
        delivery_sent: int,
        delivery_failed: int,
        delivery_uncertain: int,
        warning_count: int,
        details: dict[str, Any],
        now_utc: datetime,
    ) -> int:
        if mode not in {"live", "fixture", "test"}:
            raise ValueError("scheduler tick mode must be live, fixture, or test")
        if status not in {"ok", "degraded"}:
            raise ValueError("scheduler tick status must be ok or degraded")
        now = self._iso(now_utc)
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO scheduler_ticks(
                       mode, status, source_started, source_succeeded,
                       source_failed, delivery_sent, delivery_failed,
                       delivery_uncertain, warning_count, details_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mode,
                    status,
                    source_started,
                    source_succeeded,
                    source_failed,
                    delivery_sent,
                    delivery_failed,
                    delivery_uncertain,
                    warning_count,
                    json.dumps(details, sort_keys=True, default=str),
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def scheduler_tick_window(
        self, *, mode: str, since_utc: datetime
    ) -> dict[str, Any]:
        since = self._iso(since_utc)
        with self.connection() as connection:
            all_bounds = connection.execute(
                """SELECT COUNT(*) AS count, MIN(created_at) AS first_at,
                          MAX(created_at) AS last_at
                   FROM scheduler_ticks WHERE mode=?""",
                (mode,),
            ).fetchone()
            rows = connection.execute(
                """SELECT * FROM scheduler_ticks
                   WHERE mode=? AND created_at>=? ORDER BY created_at, tick_id""",
                (mode, since),
            ).fetchall()
        timestamps = [
            datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            for row in rows
        ]
        gaps = [
            (right - left).total_seconds() / 60
            for left, right in zip(timestamps, timestamps[1:])
        ]
        return {
            "total_ticks": int(all_bounds["count"] or 0),
            "first_tick_at": all_bounds["first_at"],
            "last_tick_at": all_bounds["last_at"],
            "window_ticks": len(rows),
            "window_degraded": sum(row["status"] == "degraded" for row in rows),
            "window_source_failures": sum(int(row["source_failed"]) for row in rows),
            "window_delivery_failures": sum(int(row["delivery_failed"]) for row in rows),
            "window_uncertain": sum(int(row["delivery_uncertain"]) for row in rows),
            "max_gap_minutes": max(gaps) if gaps else None,
        }

    def rollout_attestations(self) -> dict[str, RolloutAttestation]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM rollout_attestations ORDER BY gate"
            ).fetchall()
        return {
            str(row["gate"]): RolloutAttestation(
                gate=str(row["gate"]),
                evidence=str(row["evidence"]),
                attested_at=str(row["attested_at"]),
            )
            for row in rows
        }

    def attest_rollout_gate(
        self, gate: str, *, evidence: str, now_utc: datetime
    ) -> RolloutAttestation:
        gate_name = gate.strip()
        evidence_text = evidence.strip()
        if not gate_name or not evidence_text:
            raise ValueError("rollout gate and evidence must not be blank")
        now = self._iso(now_utc)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO rollout_attestations(gate, evidence, attested_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(gate) DO UPDATE SET
                           evidence=excluded.evidence,
                           attested_at=excluded.attested_at""",
                    (gate_name[:100], evidence_text[:500], now),
                )
                connection.execute(
                    """INSERT INTO operator_audit(
                           action, target_type, target_id, details_json, created_at
                       ) VALUES ('attest_rollout_gate', 'rollout_gate', ?, ?, ?)""",
                    (
                        gate_name[:100],
                        json.dumps({"evidence": evidence_text[:500]}),
                        now,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.rollout_attestations()[gate_name[:100]]

    def clear_rollout_attestation(self, gate: str, *, now_utc: datetime) -> bool:
        gate_name = gate.strip()
        now = self._iso(now_utc)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM rollout_attestations WHERE gate=?", (gate_name,)
                )
                if cursor.rowcount:
                    connection.execute(
                        """INSERT INTO operator_audit(
                               action, target_type, target_id, details_json, created_at
                           ) VALUES ('clear_rollout_gate', 'rollout_gate', ?, '{}', ?)""",
                        (gate_name, now),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return bool(cursor.rowcount)

    def create_daily_backup(self, *, now_utc: datetime) -> Path:
        """Create or verify one consistent SQLite backup per UTC day."""
        if not self.path.exists():
            raise AlertStoreError("cannot back up a missing alert ledger")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"alerts-daily-{now_utc.astimezone(timezone.utc).date()}.sqlite3"
        if target.exists():
            self.verify_backup(target)
            return target
        fd, temp_name = tempfile.mkstemp(
            dir=self.backup_dir, prefix=".alerts-backup-", suffix=".sqlite3"
        )
        os.close(fd)
        temp = Path(temp_name)
        try:
            with self._connect(read_only=True) as source:
                with sqlite3.connect(temp) as destination:
                    source.backup(destination)
            self.verify_backup(temp)
            os.chmod(temp, 0o600)
            os.replace(temp, target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return target

    def verify_backup(self, path: str | Path) -> dict[str, Any]:
        backup = Path(path)
        if not backup.is_file():
            raise AlertStoreError(f"alert backup does not exist: {backup}")
        try:
            uri = f"file:{backup.resolve()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
                integrity = self._integrity(connection)
                version = self._current_version(connection)
        except sqlite3.Error as exc:
            raise AlertStoreError(f"cannot verify alert backup {backup}: {exc}") from exc
        if integrity != "ok" or version != LATEST_ALERT_SCHEMA_VERSION:
            raise AlertStoreError(
                f"invalid alert backup {backup}: integrity={integrity}, version={version}"
            )
        return {"path": str(backup), "integrity": integrity, "version": version}

    def restore_drill(self, path: str | Path) -> dict[str, Any]:
        """Restore into a disposable database and verify it, never touching live state."""
        verified = self.verify_backup(path)
        fd, temp_name = tempfile.mkstemp(
            dir=self.backup_dir, prefix=".alerts-restore-drill-", suffix=".sqlite3"
        )
        os.close(fd)
        temp = Path(temp_name)
        try:
            with sqlite3.connect(Path(path)) as source:
                with sqlite3.connect(temp) as destination:
                    source.backup(destination)
            drill = self.verify_backup(temp)
        finally:
            temp.unlink(missing_ok=True)
        return {
            "source": verified["path"],
            "integrity": drill["integrity"],
            "version": drill["version"],
            "live_database_untouched": True,
        }

    def prune_history(self, *, before_utc: datetime) -> dict[str, int]:
        """Prune completed operational history; active/problem rows are retained."""
        before = self._iso(before_utc)
        removed: dict[str, int] = {}
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """DELETE FROM delivery_receipts WHERE outbox_id IN (
                           SELECT outbox_id FROM outbox
                           WHERE status IN ('sent', 'cancelled') AND created_at<?
                       )""",
                    (before,),
                )
                removed["delivery_receipts"] = cursor.rowcount
                cursor = connection.execute(
                    """DELETE FROM outbox WHERE status IN ('sent', 'cancelled')
                       AND created_at<?""",
                    (before,),
                )
                removed["outbox"] = cursor.rowcount
                cursor = connection.execute(
                    """DELETE FROM source_runs WHERE finished_at<? AND (
                           status IN ('failed', 'timed_out', 'cancelled') OR
                           (status='succeeded' AND processed_at IS NOT NULL)
                       )""",
                    (before,),
                )
                removed["source_runs"] = cursor.rowcount
                cursor = connection.execute(
                    """DELETE FROM query_scopes WHERE active=0 AND retired_at<?
                       AND NOT EXISTS (
                           SELECT 1 FROM source_runs r
                           WHERE r.query_id=query_scopes.query_id
                           AND (r.status IN ('reserved', 'running') OR r.processed_at IS NULL)
                       )""",
                    (before,),
                )
                removed["query_scopes"] = cursor.rowcount
                cursor = connection.execute(
                    """DELETE FROM listing_versions
                       WHERE NOT EXISTS (
                           SELECT 1 FROM observations o
                           WHERE o.identity_key=listing_versions.identity_key
                           AND o.version_hash=listing_versions.version_hash
                       ) AND NOT EXISTS (
                           SELECT 1 FROM outbox o
                           WHERE o.identity_key=listing_versions.identity_key
                           AND o.version_hash=listing_versions.version_hash
                       )"""
                )
                removed["listing_versions"] = cursor.rowcount
                cursor = connection.execute(
                    """DELETE FROM listing_identities
                       WHERE NOT EXISTS (
                           SELECT 1 FROM listing_versions v
                           WHERE v.identity_key=listing_identities.identity_key
                       )"""
                )
                removed["listing_identities"] = cursor.rowcount
                cursor = connection.execute(
                    "DELETE FROM scheduler_ticks WHERE created_at<?", (before,)
                )
                removed["scheduler_ticks"] = cursor.rowcount
                cursor = connection.execute(
                    "DELETE FROM operator_audit WHERE created_at<?", (before,)
                )
                removed["operator_audit"] = cursor.rowcount
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return removed

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
