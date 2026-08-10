"""Persistent delivery ledger: tracks per-(telegram_user, listing) send state.

Each row is keyed by (telegram_user_id, listing_key) so a listing already
sent to one user is never selected again for that same user, while the same
listing_key for a different user is tracked independently. Retries are
bounded: once `attempt_count` reaches `max_attempts`, a failed row is no
longer eligible for another send attempt.

No network, scheduler, or Telegram code lives here: this module only reads
and writes local SQLite state.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from chc_rental.db import Database
from chc_rental.errors import DeliveryRetryLimitExceededError
from chc_rental.models import DeliveryRecord, DeliveryStatus

DEFAULT_MAX_ATTEMPTS = 3


def _row_to_record(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(**dict(row))


class DeliveryRepository:
    def __init__(self, db: Database) -> None:
        self._conn: sqlite3.Connection = db.connection

    def _fetch(self, telegram_user_id: int, listing_key: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM delivery_ledger WHERE telegram_user_id = ? AND listing_key = ?",
            (telegram_user_id, listing_key),
        ).fetchone()

    def get(self, telegram_user_id: int, listing_key: str) -> Optional[DeliveryRecord]:
        row = self._fetch(telegram_user_id, listing_key)
        return _row_to_record(row) if row is not None else None

    def record_pending(
        self,
        telegram_user_id: int,
        profile_id: Optional[int],
        listing_key: str,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> DeliveryRecord:
        """Idempotently ensure a ledger row exists. Never resets an existing
        row's status: a listing already marked `sent` or `failed` keeps that
        state even if it is re-offered as a pending candidate."""
        existing = self._fetch(telegram_user_id, listing_key)
        if existing is not None:
            return _row_to_record(existing)
        self._conn.execute(
            "INSERT INTO delivery_ledger "
            "(telegram_user_id, profile_id, listing_key, status, attempt_count, max_attempts) "
            "VALUES (?, ?, ?, 'pending', 0, ?)",
            (telegram_user_id, profile_id, listing_key, max_attempts),
        )
        self._conn.commit()
        return _row_to_record(self._fetch(telegram_user_id, listing_key))  # type: ignore[arg-type]

    def is_eligible_for_send(self, telegram_user_id: int, listing_key: str) -> bool:
        """True when this (user, listing) has never been attempted, is still
        pending, or failed but has retry attempts remaining. False once it
        has been sent, or once its failure retries are exhausted."""
        row = self._fetch(telegram_user_id, listing_key)
        if row is None:
            return True
        if row["status"] == DeliveryStatus.SENT.value:
            return False
        if row["status"] == DeliveryStatus.FAILED.value:
            return row["attempt_count"] < row["max_attempts"]
        return True

    def mark_sent(self, telegram_user_id: int, listing_key: str) -> DeliveryRecord:
        row = self._fetch(telegram_user_id, listing_key)
        if row is None:
            raise LookupError(f"no delivery ledger row for user {telegram_user_id}, listing {listing_key!r}")
        self._conn.execute(
            "UPDATE delivery_ledger SET status = 'sent', last_error = NULL, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (row["id"],),
        )
        self._conn.commit()
        return _row_to_record(self._fetch(telegram_user_id, listing_key))  # type: ignore[arg-type]

    def mark_failed(self, telegram_user_id: int, listing_key: str, error: str = "") -> DeliveryRecord:
        row = self._fetch(telegram_user_id, listing_key)
        if row is None:
            raise LookupError(f"no delivery ledger row for user {telegram_user_id}, listing {listing_key!r}")
        if row["status"] == DeliveryStatus.FAILED.value and row["attempt_count"] >= row["max_attempts"]:
            raise DeliveryRetryLimitExceededError(
                f"user {telegram_user_id} listing {listing_key!r} already exhausted "
                f"{row['max_attempts']} retry attempt(s)"
            )
        new_attempt_count = row["attempt_count"] + 1
        self._conn.execute(
            "UPDATE delivery_ledger SET status = 'failed', attempt_count = ?, last_error = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (new_attempt_count, error, row["id"]),
        )
        self._conn.commit()
        return _row_to_record(self._fetch(telegram_user_id, listing_key))  # type: ignore[arg-type]

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM delivery_ledger GROUP BY status"
        ).fetchall()
        counts = {status.value: 0 for status in DeliveryStatus}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts
