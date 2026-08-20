"""The single gate for every config and state file.

Nothing else in this package may open these paths.  The build this replaces had
two access paths — a guarded repository layer and raw SQL that reached past it —
and every one of its worst defects came from that split: removed users stayed in
the pipeline, and a non-atomic read-modify-write silently un-tripped the daily
circuit breaker.  One gate is the fix, so keep it that way.

Every mutation is:

1. taken under an exclusive ``flock`` on a per-file lock, so two processes (the
   TUI and the daily job) cannot interleave a read-modify-write;
2. written to a temp file in the same directory and moved into place with
   ``os.replace``, which is atomic on POSIX, so a crash mid-write cannot leave a
   truncated config;
3. preceded, for config files, by a timestamped copy into ``backups/``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import unquote
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError

from chc_rental.event_store import AlertMigrationStatus, EventStore
from chc_rental.delivery_history import (
    DELIVERY_CHANNELS,
    RoutineDeliveryDay,
    RoutineDeliveryEntry,
    RoutineDeliveryItem,
    fold_routine_events,
    group_routine_days,
)
from chc_rental.models import (
    ALERT_CONFIG_SCHEMA_VERSION,
    Allowlist,
    Listing,
    Settings,
)

CONFIG_DIR = "config"
STATE_DIR = "state"
BACKUP_DIR = "backups"


class StoreError(RuntimeError):
    """Raised when a file cannot be read, parsed or validated."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%SZ")


class Store:
    """Owns one CHC_Rental data directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.config_dir = self.root / CONFIG_DIR
        self.state_dir = self.root / STATE_DIR
        self.backup_dir = self.root / BACKUP_DIR
        self._lock_dir = self.state_dir / ".locks"

    # ------------------------------------------------------------------ paths
    @property
    def allowlist_path(self) -> Path:
        return self.config_dir / "allowlist.yaml"

    @property
    def settings_path(self) -> Path:
        return self.config_dir / "settings.yaml"

    def seen_path(self, telegram_id: int) -> Path:
        return self.state_dir / "seen" / f"{telegram_id}.jsonl"

    def quota_path(self, day: date) -> Path:
        return self.state_dir / "quota" / f"{day.isoformat()}.json"

    def run_log_path(self, day: date) -> Path:
        return self.state_dir / "runs" / f"{day.isoformat()}.json"

    def rejected_path(self, day: date) -> Path:
        return self.state_dir / "rejected" / f"{day.isoformat()}.jsonl"

    def test_push_path(self, day: date) -> Path:
        return self.state_dir / "test-pushes" / f"{day.isoformat()}.jsonl"

    def routine_journal_dir(self, telegram_id: int) -> Path:
        return self.state_dir / "delivery-journal" / str(telegram_id)

    def routine_journal_path(self, telegram_id: int, local_day: date) -> Path:
        return self.routine_journal_dir(telegram_id) / f"{local_day.isoformat()}.jsonl"

    def cache_dir(self, day: date) -> Path:
        return self.state_dir / "cache" / day.isoformat()

    def cache_path(self, day: date, source: str) -> Path:
        return self.cache_dir(day) / f"{source}.json"

    def cache_metadata_path(self, day: date, source: str) -> Path:
        return self.cache_dir(day) / f"{source}.meta.json"

    @property
    def alert_db_path(self) -> Path:
        return self.state_dir / "alerts.sqlite3"

    def event_store(self) -> EventStore:
        """Return the lazy operational-ledger boundary without opening a DB."""
        return EventStore(self.alert_db_path, backup_dir=self.backup_dir)

    # ------------------------------------------------------------- primitives
    def initialize(self) -> None:
        """Create the directory skeleton and default config if absent."""
        for path in (
            self.config_dir,
            self.state_dir,
            self.backup_dir,
            self._lock_dir,
            self.state_dir / "seen",
            self.state_dir / "quota",
            self.state_dir / "runs",
            self.state_dir / "rejected",
            self.state_dir / "test-pushes",
            self.state_dir / "delivery-journal",
            self.state_dir / "cache",
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not self.allowlist_path.exists():
            self._write_yaml(self.allowlist_path, Allowlist().model_dump(mode="json"))
        if not self.settings_path.exists():
            self._write_yaml(self.settings_path, Settings().model_dump(mode="json"))

    @contextmanager
    def _locked(self, name: str) -> Iterator[None]:
        """Hold an exclusive advisory lock for the duration of the block."""
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_dir / f"{name}.lock"
        handle = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    @contextmanager
    def recipient_delivery_lock(self, telegram_id: int) -> Iterator[None]:
        """Serialize every outbound delivery path for one recipient.

        Planning and previewing are intentionally lock-free.  The daily,
        incremental, and manual Test Push paths take this lock immediately
        before transport, re-check the recipient's bank, and hold it until the
        accepted Telegram receipt has been recorded.  Different recipients do
        not block one another.
        """
        if (
            not isinstance(telegram_id, int)
            or isinstance(telegram_id, bool)
            or telegram_id <= 0
        ):
            raise ValueError("Telegram ID must be a positive integer")
        with self._locked(f"delivery-{telegram_id}"):
            yield

    @contextmanager
    def try_run_lock(self) -> Iterator[bool]:
        """Try to own the whole fetch/delivery cycle without blocking.

        Per-file locks protect individual writes, but a managed Zillow actor can
        run for minutes. This process-wide gate prevents a manual invocation
        from overlapping the scheduled cycle and paying for the same scrape.
        """
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        handle = os.open(self._lock_dir / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            if acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _atomic_append(self, path: Path, line: str) -> None:
        """Append one line durably. O_APPEND writes are atomic for small records."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(handle, (line.rstrip("\n") + "\n").encode("utf-8"))
            os.fsync(handle)
        finally:
            os.close(handle)

    def _write_yaml(self, path: Path, payload: dict[str, Any]) -> None:
        self._atomic_write(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise StoreError(f"missing config file: {path}")
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise StoreError(f"{path} is not valid YAML: {exc}") from exc
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise StoreError(f"{path} must contain a mapping at the top level")
        return loaded

    def _backup(self, path: Path) -> Optional[Path]:
        if not path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"{path.stem}-{_stamp()}{path.suffix}"
        # copyfile (not copy2) so the backup's own mtime records when it was taken.
        shutil.copyfile(path, target)
        os.chmod(target, 0o600)
        return target

    # --------------------------------------------------------------- allowlist
    def load_allowlist(self) -> Allowlist:
        try:
            return Allowlist.model_validate(self._read_yaml(self.allowlist_path))
        except ValidationError as exc:
            raise StoreError(f"{self.allowlist_path} failed validation:\n{exc}") from exc

    def save_allowlist(self, allowlist: Allowlist) -> None:
        with self._locked("allowlist"):
            self._backup(self.allowlist_path)
            self._write_yaml(self.allowlist_path, allowlist.model_dump(mode="json"))

    @contextmanager
    def edit_allowlist(self) -> Iterator[Allowlist]:
        """Read-modify-write the allowlist under one lock held throughout.

        Callers mutate the yielded object; it is validated and written on exit.
        Taking the lock around the *whole* cycle is what prevents the lost-update
        class of bug that broke the old budget ledger.
        """
        with self._locked("allowlist"):
            try:
                current = Allowlist.model_validate(self._read_yaml(self.allowlist_path))
            except ValidationError as exc:
                raise StoreError(f"{self.allowlist_path} failed validation:\n{exc}") from exc
            yield current
            validated = Allowlist.model_validate(current.model_dump())
            self._backup(self.allowlist_path)
            self._write_yaml(self.allowlist_path, validated.model_dump(mode="json"))

    # ---------------------------------------------------------------- settings
    def load_settings(self) -> Settings:
        try:
            return Settings.model_validate(self._read_yaml(self.settings_path))
        except ValidationError as exc:
            raise StoreError(f"{self.settings_path} failed validation:\n{exc}") from exc

    def save_settings(self, settings: Settings) -> None:
        with self._locked("settings"):
            self._backup(self.settings_path)
            self._write_yaml(self.settings_path, settings.model_dump(mode="json"))

    @contextmanager
    def edit_settings(self) -> Iterator[Settings]:
        """Lock, validate, back up, and atomically update global settings."""
        with self._locked("settings"):
            try:
                current = Settings.model_validate(self._read_yaml(self.settings_path))
            except ValidationError as exc:
                raise StoreError(f"{self.settings_path} failed validation:\n{exc}") from exc
            yield current
            validated = Settings.model_validate(current.model_dump())
            self._backup(self.settings_path)
            self._write_yaml(self.settings_path, validated.model_dump(mode="json"))

    # --------------------------------------------------- incremental migration
    def config_v2_status(self) -> dict[str, Any]:
        """Inspect config migration readiness without changing either file."""
        allowlist_raw = self._read_yaml(self.allowlist_path)
        settings_raw = self._read_yaml(self.settings_path)
        missing_search_ids = 0
        search_ids: list[str] = []
        people = allowlist_raw.get("people")
        if isinstance(people, list):
            for person in people:
                if not isinstance(person, dict):
                    continue
                profile = person.get("profile")
                searches = profile.get("searches") if isinstance(profile, dict) else None
                if not isinstance(searches, list):
                    continue
                for search in searches:
                    if not isinstance(search, dict):
                        continue
                    search_id = str(search.get("search_id") or "").strip()
                    if search_id:
                        search_ids.append(search_id)
                    else:
                        missing_search_ids += 1
        duplicates = sorted({item for item in search_ids if search_ids.count(item) > 1})
        allowlist_version = int(allowlist_raw.get("schema_version", 1))
        settings_version = int(settings_raw.get("schema_version", 1))
        ready = (
            allowlist_version >= ALERT_CONFIG_SCHEMA_VERSION
            and settings_version >= ALERT_CONFIG_SCHEMA_VERSION
            and missing_search_ids == 0
            and not duplicates
        )
        return {
            "target_version": ALERT_CONFIG_SCHEMA_VERSION,
            "allowlist_version": allowlist_version,
            "settings_version": settings_version,
            "missing_search_ids": missing_search_ids,
            "duplicate_search_ids": duplicates,
            "ready": ready,
        }

    def migrate_config_v2(self) -> dict[str, Any]:
        """Assign stable search IDs and persist schema-v2 defaults safely.

        Both YAML documents are validated before either is replaced. Backups are
        restored if a write fails, so the daily workflow never sees a half-
        migrated configuration.
        """
        with self._locked("allowlist"), self._locked("settings"):
            allowlist_raw = self._read_yaml(self.allowlist_path)
            settings_raw = self._read_yaml(self.settings_path)

            ids: set[str] = set()
            people = allowlist_raw.get("people")
            if isinstance(people, list):
                for person in people:
                    if not isinstance(person, dict):
                        continue
                    profile = person.get("profile")
                    searches = profile.get("searches") if isinstance(profile, dict) else None
                    if not isinstance(searches, list):
                        continue
                    for search in searches:
                        if not isinstance(search, dict):
                            continue
                        raw_id = str(search.get("search_id") or "").strip()
                        if raw_id:
                            if raw_id in ids:
                                raise StoreError(f"duplicate search_id prevents migration: {raw_id}")
                            ids.add(raw_id)
                            continue
                        new_id = str(uuid4())
                        while new_id in ids:
                            new_id = str(uuid4())
                        search["search_id"] = new_id
                        ids.add(new_id)

            allowlist_raw["schema_version"] = ALERT_CONFIG_SCHEMA_VERSION
            settings_raw["schema_version"] = ALERT_CONFIG_SCHEMA_VERSION
            try:
                allowlist = Allowlist.model_validate(allowlist_raw)
                settings = Settings.model_validate(settings_raw)
            except ValidationError as exc:
                raise StoreError(f"config schema-v2 migration failed validation:\n{exc}") from exc

            allowlist_backup = self._backup(self.allowlist_path)
            settings_backup = self._backup(self.settings_path)
            try:
                self._write_yaml(self.allowlist_path, allowlist.model_dump(mode="json"))
                self._write_yaml(self.settings_path, settings.model_dump(mode="json"))
            except BaseException:
                if allowlist_backup is not None:
                    shutil.copyfile(allowlist_backup, self.allowlist_path)
                    os.chmod(self.allowlist_path, 0o600)
                if settings_backup is not None:
                    shutil.copyfile(settings_backup, self.settings_path)
                    os.chmod(self.settings_path, 0o600)
                raise
        return self.config_v2_status()

    def alert_migration_status(self) -> AlertMigrationStatus:
        return self.event_store().migration_status()

    def migrate_alert_ledger(self) -> AlertMigrationStatus:
        return self.event_store().migrate()

    # ---------------------------------------------------- routine delivery journal
    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("routine delivery timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _routine_events(self, telegram_id: int, local_day: date) -> list[dict[str, Any]]:
        path = self.routine_journal_path(telegram_id, local_day)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _routine_entry(
        self,
        telegram_id: int,
        local_day: date,
        attempt_id: str,
    ) -> RoutineDeliveryEntry | None:
        return next(
            (
                entry
                for entry in fold_routine_events(
                    self._routine_events(telegram_id, local_day)
                )
                if entry.attempt_id == attempt_id
            ),
            None,
        )

    @staticmethod
    def _routine_attempt_identity(
        telegram_id: int,
        local_day: date,
        kind: str,
        message_text: str,
        items: list[RoutineDeliveryItem],
        discriminator: str | None,
    ) -> tuple[str, str]:
        stable = json.dumps(
            {
                "telegram_id": telegram_id,
                "local_date": local_day.isoformat(),
                "kind": kind,
                "message_sha256": hashlib.sha256(
                    message_text.encode("utf-8")
                ).hexdigest(),
                "keys": [item.key for item in items],
                "discriminator": discriminator or "",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        attempt_key = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        return f"routine-{attempt_key[:24]}", attempt_key

    def prepare_routine_delivery(
        self,
        telegram_id: int,
        *,
        display_name: str,
        timezone_name: str,
        local_day: date,
        kind: str,
        message_text: str,
        items: list[RoutineDeliveryItem],
        now_utc: datetime,
        legacy: bool = False,
        channel: str = "scheduled",
        discriminator: str | None = None,
    ) -> RoutineDeliveryEntry:
        """Durably stage one exact routine Telegram payload before transport.

        Re-entering a definitely failed attempt makes it prepared and retryable.
        Re-entering a stale ``sending`` attempt quarantines it as uncertain;
        Telegram offers no history lookup that can prove whether it arrived.
        """
        if kind not in {"listing", "notice"}:
            raise ValueError(f"unsupported routine delivery kind: {kind}")
        if channel not in DELIVERY_CHANNELS:
            raise ValueError(f"unsupported delivery channel: {channel}")
        stamp = self._aware_utc(now_utc)
        attempt_id, attempt_key = self._routine_attempt_identity(
            telegram_id,
            local_day,
            kind,
            message_text,
            items,
            discriminator,
        )
        lock_name = f"routine-journal-{telegram_id}"
        with self._locked(lock_name):
            current = self._routine_entry(telegram_id, local_day, attempt_id)
            if current is not None and current.status in {"accepted", "uncertain"}:
                return current
            if current is not None and current.status == "sending":
                self._atomic_append(
                    self.routine_journal_path(telegram_id, local_day),
                    json.dumps(
                        {
                            "schema_version": 1,
                            "attempt_id": attempt_id,
                            "state": "uncertain",
                            "event_at": stamp.isoformat(),
                            "error": (
                                "recovered an interrupted Telegram send; delivery "
                                "cannot be confirmed and was not retried"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
                recovered = self._routine_entry(telegram_id, local_day, attempt_id)
                if recovered is None:  # pragma: no cover - durable append invariant
                    raise StoreError("routine delivery recovery could not be read back")
                return recovered
            if current is None or current.status == "failed":
                payload = {
                    "display_name": display_name,
                    "timezone": timezone_name,
                    "local_date": local_day.isoformat(),
                    "kind": kind,
                    "message_text": message_text,
                    "items": [item.as_dict() for item in items],
                    "legacy": bool(legacy),
                    "channel": channel,
                }
                self._atomic_append(
                    self.routine_journal_path(telegram_id, local_day),
                    json.dumps(
                        {
                            "schema_version": 1,
                            "attempt_id": attempt_id,
                            "attempt_key": attempt_key,
                            "telegram_id": telegram_id,
                            "state": "prepared",
                            "event_at": stamp.isoformat(),
                            "payload": payload,
                        },
                        ensure_ascii=False,
                    ),
                )
            prepared = self._routine_entry(telegram_id, local_day, attempt_id)
            if prepared is None:  # pragma: no cover - durable append invariant
                raise StoreError("prepared routine delivery could not be read back")
            return prepared

    def transition_routine_delivery(
        self,
        telegram_id: int,
        local_day: date,
        attempt_id: str,
        *,
        state: str,
        now_utc: datetime,
        telegram_message_id: str | None = None,
        chat_id: str | None = None,
        error: str | None = None,
    ) -> RoutineDeliveryEntry:
        """Append one legal lifecycle transition and read its folded result."""
        stamp = self._aware_utc(now_utc)
        allowed_from = {
            "sending": {"prepared"},
            "accepted": {"sending"},
            "failed": {"sending"},
            "uncertain": {"sending"},
        }
        if state not in allowed_from:
            raise ValueError(f"unsupported routine delivery transition: {state}")
        with self._locked(f"routine-journal-{telegram_id}"):
            current = self._routine_entry(telegram_id, local_day, attempt_id)
            if current is None:
                raise StoreError(f"unknown routine delivery attempt: {attempt_id}")
            if current.status == state:
                return current
            if current.status not in allowed_from[state]:
                raise StoreError(
                    f"routine delivery {attempt_id} cannot change "
                    f"from {current.status} to {state}"
                )
            event: dict[str, Any] = {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "state": state,
                "event_at": stamp.isoformat(),
            }
            if telegram_message_id is not None:
                event["telegram_message_id"] = str(telegram_message_id)
            if chat_id is not None:
                event["chat_id"] = str(chat_id)
            if error is not None:
                event["error"] = str(error)
            self._atomic_append(
                self.routine_journal_path(telegram_id, local_day),
                json.dumps(event, ensure_ascii=False),
            )
            updated = self._routine_entry(telegram_id, local_day, attempt_id)
            if updated is None:  # pragma: no cover - durable append invariant
                raise StoreError("routine delivery transition could not be read back")
            return updated

    def annotate_routine_delivery(
        self,
        telegram_id: int,
        local_day: date,
        attempt_id: str,
        *,
        now_utc: datetime,
        message_text: str | None = None,
        items: list[RoutineDeliveryItem] | None = None,
        channel: str | None = None,
        error: str | None = None,
    ) -> RoutineDeliveryEntry:
        """Append recovered historical detail without rewriting journal evidence."""
        if channel is not None and channel not in DELIVERY_CHANNELS:
            raise ValueError(f"unsupported delivery channel: {channel}")
        payload_patch: dict[str, Any] = {}
        if message_text is not None:
            payload_patch["message_text"] = message_text
        if items is not None:
            payload_patch["items"] = [item.as_dict() for item in items]
        if channel is not None:
            payload_patch["channel"] = channel
        if error is not None:
            payload_patch["error"] = error
        with self._locked(f"routine-journal-{telegram_id}"):
            current = self._routine_entry(telegram_id, local_day, attempt_id)
            if current is None:
                raise StoreError(f"unknown routine delivery attempt: {attempt_id}")
            if not payload_patch:
                return current
            if current.status not in {"accepted", "uncertain"}:
                raise StoreError(
                    f"routine delivery {attempt_id} cannot be annotated from "
                    f"{current.status}"
                )
            self._atomic_append(
                self.routine_journal_path(telegram_id, local_day),
                json.dumps(
                    {
                        "schema_version": 1,
                        "attempt_id": attempt_id,
                        "state": "annotated",
                        "event_at": self._aware_utc(now_utc).isoformat(),
                        "payload_patch": payload_patch,
                    },
                    ensure_ascii=False,
                ),
            )
            updated = self._routine_entry(telegram_id, local_day, attempt_id)
            if updated is None:  # pragma: no cover - durable append invariant
                raise StoreError("routine delivery annotation could not be read back")
            return updated

    def _routine_entries_since(
        self,
        telegram_id: int,
        *,
        cutoff_utc: datetime | None = None,
    ) -> list[RoutineDeliveryEntry]:
        directory = self.routine_journal_dir(telegram_id)
        if not directory.exists():
            return []
        entries: list[RoutineDeliveryEntry] = []
        for path in sorted(directory.glob("*.jsonl")):
            try:
                local_day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            entries.extend(
                fold_routine_events(self._routine_events(telegram_id, local_day))
            )
        if cutoff_utc is not None:
            entries = [
                entry
                for entry in entries
                if entry.updated_at.astimezone(timezone.utc) >= cutoff_utc
            ]
        return entries

    def routine_accepted_keys(
        self,
        telegram_id: int,
        *,
        now_utc: datetime | None = None,
        retention_days: int | None = None,
    ) -> set[str]:
        days = (
            self.load_settings().seen_retention_days
            if retention_days is None
            else retention_days
        )
        cutoff = None
        if days > 0:
            now = self._aware_utc(now_utc or _utc_now())
            cutoff = now - timedelta(days=days)
        return {
            item.key
            for entry in self._routine_entries_since(
                telegram_id,
                cutoff_utc=cutoff,
            )
            if (
                entry.status == "accepted"
                and entry.kind == "listing"
                and entry.channel == "scheduled"
            )
            for item in entry.items
        }

    def routine_delivery_days(
        self,
        telegram_id: int,
        *,
        now_utc: datetime | None = None,
    ) -> list[RoutineDeliveryDay]:
        """Return the visible one-year routine diary for one current ID."""
        self.import_legacy_routine_history(telegram_id)
        settings = self.load_settings()
        now = self._aware_utc(now_utc or _utc_now())
        cutoff = now - timedelta(days=settings.delivery_history_retention_days)
        return group_routine_days(
            self._routine_entries_since(telegram_id, cutoff_utc=cutoff)
        )

    def reconcile_routine_seen(
        self,
        telegram_id: int,
        *,
        now_utc: datetime | None = None,
    ) -> int:
        """Repair the compact seen projection from accepted routine receipts."""
        now = self._aware_utc(now_utc or _utc_now())
        repaired = 0
        retention_days = self.load_settings().seen_retention_days
        cutoff = (
            now - timedelta(days=retention_days)
            if retention_days > 0
            else None
        )
        for entry in self._routine_entries_since(
            telegram_id,
            cutoff_utc=cutoff,
        ):
            if (
                entry.status != "accepted"
                or entry.kind != "listing"
                or entry.channel != "scheduled"
            ):
                continue
            for item in entry.items:
                repaired += int(
                    self.mark_seen(
                        telegram_id,
                        item.key,
                        search_name=item.search_name,
                        url=item.url,
                        now_utc=entry.updated_at,
                        channel="scheduled",
                        telegram_message_id=entry.telegram_message_id,
                        chat_id=entry.chat_id,
                    )
                )
        return repaired

    def import_legacy_routine_history(self, telegram_id: int) -> int:
        """Best-effort, idempotent import of accepted legacy scheduled rows."""
        from chc_rental.dedup import upgrade_seen_key

        path = self.seen_path(telegram_id)
        if not path.exists():
            return 0
        person = self.load_allowlist().get(telegram_id)
        if person is None:
            return 0
        try:
            local_zone = ZoneInfo(person.profile.timezone)
        except Exception:
            local_zone = timezone.utc
        cached = self._cached_listings_by_key()
        existing = self._routine_entries_since(telegram_id)
        imported = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    continue
                if raw.get("channel", "scheduled") != "scheduled":
                    continue
                stamp = datetime.fromisoformat(str(raw["sent_at"]))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                stamp = stamp.astimezone(timezone.utc)
                key = str(raw["key"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            local_day = stamp.astimezone(local_zone).date()
            kind = "notice" if key.startswith("notice:") else "listing"
            if any(
                entry.status == "accepted"
                and entry.kind == kind
                and entry.updated_at.astimezone(timezone.utc) == stamp
                and (
                    kind == "notice"
                    or any(item.key == key for item in entry.items)
                )
                for entry in existing
            ):
                continue
            items: list[RoutineDeliveryItem] = []
            message_text = "No new rentals matched your searches today." if kind == "notice" else ""
            if kind == "listing":
                listing = cached.get(upgrade_seen_key(key))
                items.append(
                    self._legacy_routine_item(
                        key,
                        search_name=str(raw.get("search") or ""),
                        url=str(raw.get("url") or ""),
                        listing=listing,
                    )
                )
            before = self._routine_attempt_identity(
                telegram_id,
                local_day,
                kind,
                message_text,
                items,
                stamp.isoformat(),
            )[0]
            if self._routine_entry(telegram_id, local_day, before) is not None:
                continue
            entry = self.prepare_routine_delivery(
                telegram_id,
                display_name=person.display_name,
                timezone_name=person.profile.timezone,
                local_day=local_day,
                kind=kind,
                message_text=message_text,
                items=items,
                now_utc=stamp,
                legacy=True,
                discriminator=stamp.isoformat(),
            )
            entry = self.transition_routine_delivery(
                telegram_id,
                local_day,
                entry.attempt_id,
                state="sending",
                now_utc=stamp,
            )
            imported_entry = self.transition_routine_delivery(
                telegram_id,
                local_day,
                entry.attempt_id,
                state="accepted",
                now_utc=stamp,
                telegram_message_id=(
                    str(raw["telegram_message_id"])
                    if raw.get("telegram_message_id") is not None
                    else None
                ),
                chat_id=(
                    str(raw["chat_id"])
                    if raw.get("chat_id") is not None
                    else str(telegram_id)
                ),
            )
            existing.append(imported_entry)
            imported += 1
        return imported

    def _cached_listings_by_key(self) -> dict[str, Listing]:
        from chc_rental.dedup import dedup_key, upgrade_seen_key

        found: dict[str, Listing] = {}
        for path in sorted(self.state_dir.glob("cache/*/*.json"), reverse=True):
            try:
                raw_records = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, TypeError):
                continue
            if not isinstance(raw_records, list):
                continue
            for raw in raw_records:
                try:
                    listing = Listing.model_validate(raw)
                except Exception:
                    continue
                found.setdefault(upgrade_seen_key(dedup_key(listing)), listing)
        return found

    @staticmethod
    def _legacy_routine_item(
        key: str,
        *,
        search_name: str,
        url: str,
        listing: Listing | None,
    ) -> RoutineDeliveryItem:
        if listing is not None:
            return RoutineDeliveryItem(
                key=key,
                search_name=search_name,
                url=url or listing.url,
                address=listing.address,
                unit=listing.unit,
                city=listing.city,
                district=listing.district,
                price=listing.price,
                beds=listing.beds,
                baths=listing.baths,
                sqft=listing.sqft,
                source=listing.source,
            )
        address = None
        unit = None
        city = None
        district = None
        parts = key.split(":")
        if len(parts) == 6 and parts[0] == "v3":
            city = unquote(parts[2]) or None
            district = unquote(parts[3]) or None
            address = unquote(parts[4]) or None
            unit = unquote(parts[5]) or None
        return RoutineDeliveryItem(
            key=key,
            search_name=search_name,
            url=url,
            address=address,
            unit=unit,
            city=city,
            district=district,
        )

    # -------------------------------------------------------------- seen ledger
    @staticmethod
    def _delivery_record_is_active(
        record: dict[str, Any],
        *,
        cutoff: datetime | None,
    ) -> bool:
        """Return whether a non-repeat delivery still suppresses a listing."""
        if record.get("repeat_override") is True:
            return False
        if cutoff is None:
            return True
        try:
            stamp = datetime.fromisoformat(str(record["sent_at"]))
        except (KeyError, TypeError, ValueError):
            # Preserve the old fail-safe: an unparseable delivery remains seen
            # until an operator repairs or removes the row.
            return True
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc) >= cutoff

    def _seen_keys_from_path(
        self,
        path: Path,
        *,
        cutoff: datetime | None = None,
    ) -> set[str]:
        if not path.exists():
            return set()
        keys: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                key = record["key"]
            except (json.JSONDecodeError, KeyError, TypeError):
                # A torn or hand-edited line must not hide everything after it.
                continue
            if not isinstance(record, dict) or not isinstance(key, str):
                continue
            if self._delivery_record_is_active(record, cutoff=cutoff):
                keys.add(key)
        return keys

    def seen_keys(self, telegram_id: int) -> set[str]:
        """Return suppressing listing keys, excluding audited repeat sends."""
        return self._seen_keys_from_path(self.seen_path(telegram_id))

    def active_seen_keys(
        self,
        telegram_id: int,
        *,
        now_utc: Optional[datetime] = None,
    ) -> set[str]:
        """Return keys inside the configured per-recipient retention window."""
        settings = self.load_settings()
        cutoff: datetime | None = None
        if settings.seen_retention_days > 0:
            now = (now_utc or _utc_now()).astimezone(timezone.utc)
            cutoff = datetime.combine(
                now.date() - timedelta(days=settings.seen_retention_days),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
        return self._seen_keys_from_path(
            self.seen_path(telegram_id),
            cutoff=cutoff,
        ) | self.routine_accepted_keys(
            telegram_id,
            now_utc=now_utc,
            retention_days=settings.seen_retention_days,
        )

    def has_seen(self, telegram_id: int, key: str) -> bool:
        return key in self.seen_keys(telegram_id)

    def mark_seen(
        self,
        telegram_id: int,
        key: str,
        *,
        search_name: str,
        url: str,
        now_utc: Optional[datetime] = None,
        channel: str = "scheduled",
        telegram_message_id: str | None = None,
        chat_id: str | None = None,
        repeat_override: bool = False,
    ) -> bool:
        """Record a delivered listing. Only ever called AFTER a confirmed send.

        ``now_utc`` keeps the ledger stamp coherent with the run's simulated
        instant when the CLI is driven with ``--now``; the due-gate compares
        this stamp's local date against the planning clock, so the two must
        tell the same time.
        """
        return self.record_delivery(
            telegram_id,
            key,
            search_name=search_name,
            url=url,
            now_utc=now_utc,
            channel=channel,
            telegram_message_id=telegram_message_id,
            chat_id=chat_id,
            repeat_override=repeat_override,
        )

    def record_delivery(
        self,
        telegram_id: int,
        key: str,
        *,
        search_name: str,
        url: str,
        now_utc: Optional[datetime] = None,
        channel: str,
        telegram_message_id: str | None = None,
        chat_id: str | None = None,
        repeat_override: bool = False,
    ) -> bool:
        """Append one confirmed Telegram delivery to the recipient's bank.

        Normal deliveries suppress the same normalized property for the
        configured retention window.  An explicit repeat is retained as an
        audit event but is deliberately excluded from suppression, so it does
        not restart the original 90-day clock.
        """
        if channel not in {"scheduled", "test", "incremental"}:
            raise ValueError(f"unsupported delivery channel: {channel}")
        stamp = now_utc.astimezone(timezone.utc) if now_utc else _utc_now()
        record = {
            "key": key,
            "search": search_name,
            "url": url,
            "sent_at": stamp.isoformat(),
            "channel": channel,
            "repeat_override": bool(repeat_override),
        }
        if telegram_message_id is not None:
            record["telegram_message_id"] = str(telegram_message_id)
        if chat_id is not None:
            record["chat_id"] = str(chat_id)
        settings = self.load_settings()
        cutoff: datetime | None = None
        if settings.seen_retention_days > 0:
            cutoff = datetime.combine(
                stamp.date() - timedelta(days=settings.seen_retention_days),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
        with self._locked(f"seen-{telegram_id}"):
            path = self.seen_path(telegram_id)
            if (
                not repeat_override
                and key in self._seen_keys_from_path(path, cutoff=cutoff)
            ):
                return False
            self._atomic_append(
                self.seen_path(telegram_id),
                json.dumps(record, ensure_ascii=False),
            )
        return True

    def mark_notified(self, telegram_id: int, *, now_utc: Optional[datetime] = None) -> None:
        """Record a delivered no-results notice so the due-gate advances.

        Written to the same per-person ledger `last_sent_at` reads. Without
        this stamp a notice-only day never advances the gate and the frequent
        delivery checker repeats the notice until midnight. The ``notice:``
        key prefix cannot collide with listing keys (those start ``v3:``), so
        `seen_keys` stays safe to use for listing dedup.
        """
        stamp = now_utc.astimezone(timezone.utc) if now_utc else _utc_now()
        record = {
            "key": f"notice:{stamp.date().isoformat()}",
            "search": "",
            "url": "",
            "sent_at": stamp.isoformat(),
            "channel": "scheduled",
            "repeat_override": False,
        }
        with self._locked(f"seen-{telegram_id}"):
            self._atomic_append(self.seen_path(telegram_id), json.dumps(record, ensure_ascii=False))

    def last_sent_at(self, telegram_id: int) -> Optional[datetime]:
        path = self.seen_path(telegram_id)
        if not path.exists():
            return None
        latest: Optional[datetime] = None
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("channel") == "test" or record.get("repeat_override") is True:
                    continue
                stamp = datetime.fromisoformat(record["sent_at"])
            except (
                AttributeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ):
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if latest is None or stamp > latest:
                latest = stamp
        for entry in self._routine_entries_since(telegram_id):
            if entry.status != "accepted":
                continue
            stamp = entry.updated_at
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if latest is None or stamp > latest:
                latest = stamp
        return latest

    def purge_seen_ledgers(self, telegram_ids: set[int]) -> int:
        """Remove daily seen/delivery gates for the requested recipients.

        The per-recipient advisory locks serialize this with daily delivery
        writes. IDs are explicit and validated so this can never broaden into
        a directory-level deletion.
        """
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in telegram_ids
        ):
            raise ValueError("Telegram IDs must be positive integers")
        removed = 0
        for telegram_id in sorted(telegram_ids):
            with self._locked(f"seen-{telegram_id}"):
                path = self.seen_path(telegram_id)
                if path.exists():
                    path.unlink()
                    removed += 1
        return removed

    # -------------------------------------------------------------------- quota
    def quota_used(self, day: date, source: str) -> int:
        path = self.quota_path(day)
        if not path.exists():
            return 0
        try:
            return int(json.loads(path.read_text(encoding="utf-8")).get(source, 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            return 0

    def quota_total(self, day: date) -> int:
        path = self.quota_path(day)
        if not path.exists():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return 0
        return sum(int(v) for v in payload.values() if isinstance(v, int))

    def reserve_request(self, day: date, source: str, *, per_source_limit: int, global_limit: int) -> bool:
        """Atomically claim one request against both ceilings.

        Returns True only if the claim succeeded. The read, the check and the
        write all happen inside one lock, so two workers cannot both see room
        for the last request — the exact race that let the old ledger overspend.
        """
        with self._locked("quota"):
            path = self.quota_path(day)
            payload: dict[str, int] = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        payload = {k: int(v) for k, v in loaded.items() if isinstance(v, int)}
                except (json.JSONDecodeError, TypeError, ValueError):
                    payload = {}
            used_source = payload.get(source, 0)
            used_total = sum(payload.values())
            if used_source >= per_source_limit or used_total >= global_limit:
                return False
            payload[source] = used_source + 1
            self._atomic_write(path, json.dumps(payload, indent=2, sort_keys=True))
            return True

    # ------------------------------------------------------- rejects and runs
    def record_rejected(self, day: date, *, source: str, reason: str, raw: Any) -> None:
        """Keep an unusable record for operator review instead of dropping it.

        One malformed record must never abort a batch, and must never vanish.
        The delivery checker revalidates a day cache repeatedly, so the same
        source record/reason is fingerprinted and retained once per run day
        instead of being appended on every check.
        """
        fingerprint = self._rejected_fingerprint(source=source, reason=reason, raw=raw)
        record = {
            "at": _utc_now().isoformat(),
            "source": source,
            "reason": reason,
            "raw": raw,
            "fingerprint": fingerprint,
        }
        with self._locked("rejected"):
            path = self.rejected_path(day)
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        existing = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(existing, dict):
                        continue
                    existing_fingerprint = existing.get("fingerprint")
                    if not existing_fingerprint:
                        try:
                            existing_fingerprint = self._rejected_fingerprint(
                                source=existing["source"],
                                reason=existing["reason"],
                                raw=existing["raw"],
                            )
                        except (KeyError, TypeError):
                            continue
                    if existing_fingerprint == fingerprint:
                        return
            self._atomic_append(
                path, json.dumps(record, ensure_ascii=False, default=str)
            )

    @staticmethod
    def _rejected_fingerprint(*, source: str, reason: str, raw: Any) -> str:
        stable = json.dumps(
            {"source": source, "reason": reason, "raw": raw},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def rejected_count(self, day: date) -> int:
        path = self.rejected_path(day)
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

    def write_run_log(self, day: date, payload: dict[str, Any]) -> None:
        with self._locked("runs"):
            self._atomic_write(
                self.run_log_path(day), json.dumps(payload, indent=2, sort_keys=True, default=str)
            )

    def record_run(self, now_utc: datetime, payload: dict[str, Any]) -> Path:
        """Write one run log per RUN, not per day.

        The hourly job used to overwrite the day file: the 19:00 run that
        delivered was erased by the 20:00 run that did nothing. Per-run files
        keep the history; `latest_run_log` still returns the newest because
        ``<date>T<time>Z.json`` sorts after ``<date>.json``.
        """
        stamp = now_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        path = self.state_dir / "runs" / f"{stamp}.json"
        with self._locked("runs"):
            self._atomic_write(path, json.dumps(payload, indent=2, sort_keys=True, default=str))
        return path

    def read_run_log(self, day: date) -> Optional[dict[str, Any]]:
        path = self.run_log_path(day)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def latest_run_log(
        self, *, kind: Optional[str] = None, scan_limit: int = 50
    ) -> Optional[dict[str, Any]]:
        """Newest run record, optionally restricted to one record ``kind``.

        The daily job writes a scrape record and a delivery record on every
        tick, so an unfiltered "previous run" alternates between two different
        shapes. Any caller comparing a failure signature across runs must
        compare like with like, or the signature flips every tick and an alert
        deduped against it fires forever. ``scan_limit`` bounds the walk: the
        matching record is a tick or two back, never hundreds.
        """
        runs = sorted((self.state_dir / "runs").glob("*.json")) if self.state_dir.exists() else []
        for path in reversed(runs[-scan_limit:] if scan_limit > 0 else runs):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if kind is not None and (
                not isinstance(payload, dict) or payload.get("kind") != kind
            ):
                continue
            return payload
        return None

    def record_test_push(self, now_utc: datetime, payload: dict[str, Any]) -> Path:
        """Append a receipt-only manual-push audit record.

        The allowlist/preferences and message body stay in their existing
        sources of truth.  This record intentionally retains only identifiers,
        a preference fingerprint, outcome, and Telegram receipts.
        """
        stamp = now_utc.astimezone(timezone.utc)
        path = self.test_push_path(stamp.date())
        allowed = {
            "attempt_id",
            "telegram_id",
            "display_name",
            "preference_count",
            "listing_count",
            "cache_date",
            "preference_fingerprint",
            "parts_total",
            "parts_accepted",
            "status",
            "message_ids",
            "chat_ids",
            "listing_keys",
            "repeat_override",
            "error",
        }
        record = {key: payload.get(key) for key in allowed}
        record["attempted_at"] = stamp.isoformat()
        with self._locked("test-pushes"):
            self._atomic_append(path, json.dumps(record, ensure_ascii=False, default=str))
        return path

    # -------------------------------------------------------------------- cache
    def cache_raw(
        self,
        day: date,
        source: str,
        payload: Any,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Path:
        target = self.cache_path(day, source)
        metadata_path = self.cache_metadata_path(day, source)
        # Invalidate metadata before replacing data. If the process crashes
        # between the two atomic writes, the unscoped payload is refetched
        # instead of being trusted under stale scope/coverage metadata.
        metadata_path.unlink(missing_ok=True)
        self._atomic_write(target, json.dumps(payload, indent=2, default=str))
        if metadata is not None:
            envelope = {"schema_version": 1, **metadata}
            self._atomic_write(
                metadata_path,
                json.dumps(envelope, indent=2, sort_keys=True, default=str),
            )
        return target

    def load_cache_metadata(self, day: date, source: str) -> Optional[dict[str, Any]]:
        path = self.cache_metadata_path(day, source)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return None
        return payload

    def load_cached(
        self,
        day: date,
        source: str,
        *,
        expected_query_scope: Optional[list[dict[str, str]]] = None,
    ) -> Optional[Any]:
        path = self.cache_path(day, source)
        if not path.exists():
            return None
        if expected_query_scope is not None:
            metadata = self.load_cache_metadata(day, source)
            if metadata is None or metadata.get("query_scope") != expected_query_scope:
                return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    # ---------------------------------------------------------------- retention
    def prune(self, *, today: date, settings: Settings) -> dict[str, int]:
        """Delete state older than the configured retention. Returns counts removed."""
        removed = {
            "quota": 0,
            "runs": 0,
            "rejected": 0,
            "test_pushes": 0,
            "cache": 0,
            "backups": 0,
            "seen": 0,
            "delivery_history": 0,
        }

        def _dated_cleanup(directory: Path, days: int, bucket: str, suffix: str) -> None:
            if not directory.exists():
                return
            cutoff = today - timedelta(days=days)
            for path in directory.iterdir():
                stem = path.name[: -len(suffix)] if suffix and path.name.endswith(suffix) else path.name
                try:
                    # stem[:10] so per-run logs (2026-08-11T072106Z) date-parse too.
                    stamp = date.fromisoformat(stem[:10])
                except ValueError:
                    continue
                if stamp < cutoff:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
                    removed[bucket] += 1

        _dated_cleanup(self.state_dir / "quota", settings.cache_retention_days, "quota", ".json")
        _dated_cleanup(self.state_dir / "runs", settings.rejected_retention_days, "runs", ".json")
        _dated_cleanup(
            self.state_dir / "rejected", settings.rejected_retention_days, "rejected", ".jsonl"
        )
        _dated_cleanup(
            self.state_dir / "test-pushes",
            settings.rejected_retention_days,
            "test_pushes",
            ".jsonl",
        )
        _dated_cleanup(self.state_dir / "cache", settings.cache_retention_days, "cache", "")

        if self.backup_dir.exists():
            cutoff_dt = _utc_now() - timedelta(days=settings.backup_retention_days)
            for path in self.backup_dir.iterdir():
                if path.is_file() and datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ) < cutoff_dt:
                    path.unlink(missing_ok=True)
                    removed["backups"] += 1

        removed["seen"] = self._prune_seen(today, settings.seen_retention_days)
        removed["delivery_history"] = self._prune_routine_journal(
            today,
            settings.delivery_history_retention_days,
        )
        self._trim_daily_log()
        return removed

    def _prune_routine_journal(self, today: date, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        root = self.state_dir / "delivery-journal"
        if not root.exists():
            return 0
        cutoff = today - timedelta(days=retention_days)
        removed = 0
        for recipient_dir in root.iterdir():
            if not recipient_dir.is_dir():
                continue
            try:
                telegram_id = int(recipient_dir.name)
            except ValueError:
                continue
            with self._locked(f"routine-journal-{telegram_id}"):
                for path in recipient_dir.glob("*.jsonl"):
                    try:
                        local_day = date.fromisoformat(path.stem)
                    except ValueError:
                        continue
                    if local_day < cutoff:
                        path.unlink(missing_ok=True)
                        removed += 1
        return removed

    def _prune_seen(self, today: date, retention_days: int) -> int:
        """Drop seen-ledger records older than the retention window.

        This is what makes ``seen_retention_days`` real (Decision #5): after
        the window, a still-listed rental may notify again — accepted. Each
        person's file is rewritten atomically under their ledger lock, and a
        retention of 0 is treated as "keep forever" so a bad config cannot
        wipe every ledger in one prune.
        """
        if retention_days <= 0:
            return 0
        seen_dir = self.state_dir / "seen"
        if not seen_dir.exists():
            return 0
        cutoff = datetime.combine(today - timedelta(days=retention_days), datetime.min.time(),
                                  tzinfo=timezone.utc)
        dropped = 0
        for path in seen_dir.glob("*.jsonl"):
            with self._locked(f"seen-{path.stem}"):
                kept: list[str] = []
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        stamp = datetime.fromisoformat(json.loads(line)["sent_at"])
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        kept.append(line)  # keep what cannot be judged
                        continue
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    if stamp < cutoff:
                        dropped += 1
                    else:
                        kept.append(line)
                self._atomic_write(path, "".join(f"{line}\n" for line in kept))
        return dropped

    def _trim_daily_log(self, *, max_bytes: int = 512_000, keep_lines: int = 2000) -> None:
        """Stop state/daily.log (launchd append target) growing without bound."""
        log = self.state_dir / "daily.log"
        try:
            if not log.is_file() or log.stat().st_size <= max_bytes:
                return
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-keep_lines:]
            self._atomic_write(log, "".join(f"{line}\n" for line in lines))
        except OSError:
            return


def listing_from_raw(raw: Any) -> Listing:
    """Validate one raw source record into a `Listing`, raising on bad input."""
    return Listing.model_validate(raw)
