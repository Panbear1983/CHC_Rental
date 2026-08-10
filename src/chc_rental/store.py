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
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml
from pydantic import ValidationError

from chc_rental.models import Allowlist, Listing, Settings

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

    def cache_dir(self, day: date) -> Path:
        return self.state_dir / "cache" / day.isoformat()

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

    # -------------------------------------------------------------- seen ledger
    def seen_keys(self, telegram_id: int) -> set[str]:
        path = self.seen_path(telegram_id)
        if not path.exists():
            return set()
        keys: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                keys.add(json.loads(line)["key"])
            except (json.JSONDecodeError, KeyError, TypeError):
                # A torn or hand-edited line must not hide everything after it.
                continue
        return keys

    def has_seen(self, telegram_id: int, key: str) -> bool:
        return key in self.seen_keys(telegram_id)

    def mark_seen(self, telegram_id: int, key: str, *, search_name: str, url: str) -> None:
        """Record a delivered listing. Only ever called AFTER a confirmed send."""
        record = {
            "key": key,
            "search": search_name,
            "url": url,
            "sent_at": _utc_now().isoformat(),
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
                stamp = datetime.fromisoformat(json.loads(line)["sent_at"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if latest is None or stamp > latest:
                latest = stamp
        return latest

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
        """
        record = {
            "at": _utc_now().isoformat(),
            "source": source,
            "reason": reason,
            "raw": raw,
        }
        with self._locked("rejected"):
            self._atomic_append(
                self.rejected_path(day), json.dumps(record, ensure_ascii=False, default=str)
            )

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

    def read_run_log(self, day: date) -> Optional[dict[str, Any]]:
        path = self.run_log_path(day)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def latest_run_log(self) -> Optional[dict[str, Any]]:
        runs = sorted((self.state_dir / "runs").glob("*.json")) if self.state_dir.exists() else []
        for path in reversed(runs):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return None

    # -------------------------------------------------------------------- cache
    def cache_raw(self, day: date, source: str, payload: Any) -> Path:
        target = self.cache_dir(day) / f"{source}.json"
        self._atomic_write(target, json.dumps(payload, indent=2, default=str))
        return target

    def load_cached(self, day: date, source: str) -> Optional[Any]:
        path = self.cache_dir(day) / f"{source}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    # ---------------------------------------------------------------- retention
    def prune(self, *, today: date, settings: Settings) -> dict[str, int]:
        """Delete state older than the configured retention. Returns counts removed."""
        removed = {"quota": 0, "runs": 0, "rejected": 0, "cache": 0, "backups": 0}

        def _dated_cleanup(directory: Path, days: int, bucket: str, suffix: str) -> None:
            if not directory.exists():
                return
            cutoff = today - timedelta(days=days)
            for path in directory.iterdir():
                stem = path.name[: -len(suffix)] if suffix and path.name.endswith(suffix) else path.name
                try:
                    stamp = date.fromisoformat(stem)
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
        _dated_cleanup(self.state_dir / "cache", settings.cache_retention_days, "cache", "")

        if self.backup_dir.exists():
            cutoff_dt = _utc_now() - timedelta(days=settings.backup_retention_days)
            for path in self.backup_dir.iterdir():
                if path.is_file() and datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ) < cutoff_dt:
                    path.unlink(missing_ok=True)
                    removed["backups"] += 1
        return removed


def listing_from_raw(raw: Any) -> Listing:
    """Validate one raw source record into a `Listing`, raising on bad input."""
    return Listing.model_validate(raw)
