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

    def cache_path(self, day: date, source: str) -> Path:
        return self.cache_dir(day) / f"{source}.json"

    def cache_metadata_path(self, day: date, source: str) -> Path:
        return self.cache_dir(day) / f"{source}.meta.json"

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

    def mark_seen(
        self,
        telegram_id: int,
        key: str,
        *,
        search_name: str,
        url: str,
        now_utc: Optional[datetime] = None,
    ) -> None:
        """Record a delivered listing. Only ever called AFTER a confirmed send.

        ``now_utc`` keeps the ledger stamp coherent with the run's simulated
        instant when the CLI is driven with ``--now``; the due-gate compares
        this stamp's local date against the planning clock, so the two must
        tell the same time.
        """
        stamp = now_utc.astimezone(timezone.utc) if now_utc else _utc_now()
        record = {
            "key": key,
            "search": search_name,
            "url": url,
            "sent_at": stamp.isoformat(),
        }
        with self._locked(f"seen-{telegram_id}"):
            self._atomic_append(self.seen_path(telegram_id), json.dumps(record, ensure_ascii=False))

    def mark_notified(self, telegram_id: int, *, now_utc: Optional[datetime] = None) -> None:
        """Record a delivered no-results notice so the due-gate advances.

        Written to the same per-person ledger `last_sent_at` reads. Without
        this stamp a notice-only day never advances the gate and the hourly
        runner repeats the notice every hour until midnight. The ``notice:``
        key prefix cannot collide with listing keys (those start ``v3:``), so
        `seen_keys` stays safe to use for listing dedup.
        """
        stamp = now_utc.astimezone(timezone.utc) if now_utc else _utc_now()
        record = {
            "key": f"notice:{stamp.date().isoformat()}",
            "search": "",
            "url": "",
            "sent_at": stamp.isoformat(),
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
        The hourly runner revalidates a day cache repeatedly, so the same
        source record/reason is fingerprinted and retained once per UTC day
        instead of being appended 24 times.
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

    def latest_run_log(self) -> Optional[dict[str, Any]]:
        runs = sorted((self.state_dir / "runs").glob("*.json")) if self.state_dir.exists() else []
        for path in reversed(runs):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return None

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
        removed = {"quota": 0, "runs": 0, "rejected": 0, "cache": 0, "backups": 0, "seen": 0}

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
        self._trim_daily_log()
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
