"""Owner-controlled allowlist, preference profile, and audit event repositories.

All profile access is gated on `AllowlistRepository.is_allowlisted`. There is no
network entry point here and no self-enrollment path: the allowlist is only ever
populated by code that already runs under the owner's control (e.g. a local TUI
in a later phase). Denied and cross-user access attempts are recorded as audit
events rather than silently rejected.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from chc_rental.db import Database
from chc_rental.errors import (
    DuplicateProfileNameError,
    NotAllowlistedError,
    ProfileAccessDeniedError,
    ProfileNotFoundError,
)
from chc_rental.models import (
    AllowlistedUser,
    AuditEvent,
    PreferenceProfile,
    PreferenceProfileCreate,
    PreferenceProfileUpdate,
)


def _row_to_user(row: sqlite3.Row) -> AllowlistedUser:
    data = dict(row)
    data["active"] = bool(data["active"])
    return AllowlistedUser(**data)


class AllowlistRepository:
    def __init__(self, db: Database) -> None:
        self._conn: sqlite3.Connection = db.connection

    def add_user(self, telegram_user_id: int, display_name: str) -> AllowlistedUser:
        try:
            cursor = self._conn.execute(
                "INSERT INTO allowlisted_users (telegram_user_id, display_name) VALUES (?, ?)",
                (telegram_user_id, display_name),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"telegram_user_id {telegram_user_id} is already allowlisted"
            ) from exc
        return self.get_user(telegram_user_id)  # type: ignore[return-value]

    def is_allowlisted(self, telegram_user_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM allowlisted_users WHERE telegram_user_id = ? AND active = 1",
            (telegram_user_id,),
        ).fetchone()
        return row is not None

    def get_user(self, telegram_user_id: int) -> Optional[AllowlistedUser]:
        row = self._conn.execute(
            "SELECT * FROM allowlisted_users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_user(row)

    def list_users(self) -> list[AllowlistedUser]:
        rows = self._conn.execute(
            "SELECT * FROM allowlisted_users ORDER BY id"
        ).fetchall()
        return [_row_to_user(row) for row in rows]

    def deactivate_user(self, telegram_user_id: int) -> AllowlistedUser:
        if self.get_user(telegram_user_id) is None:
            raise ValueError(f"telegram_user_id {telegram_user_id} is not allowlisted")
        self._conn.execute(
            "UPDATE allowlisted_users SET active = 0 WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        self._conn.commit()
        return self.get_user(telegram_user_id)  # type: ignore[return-value]


class AuditRepository:
    def __init__(self, db: Database) -> None:
        self._conn: sqlite3.Connection = db.connection

    def record(
        self,
        event_type: str,
        telegram_user_id: Optional[int] = None,
        profile_id: Optional[int] = None,
        detail: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO audit_events (event_type, telegram_user_id, profile_id, detail) "
            "VALUES (?, ?, ?, ?)",
            (event_type, telegram_user_id, profile_id, detail),
        )
        self._conn.commit()

    def list_events(self, telegram_user_id: Optional[int] = None) -> list[AuditEvent]:
        if telegram_user_id is None:
            rows = self._conn.execute(
                "SELECT * FROM audit_events ORDER BY id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit_events WHERE telegram_user_id = ? ORDER BY id",
                (telegram_user_id,),
            ).fetchall()
        return [AuditEvent(**dict(row)) for row in rows]


def _row_to_profile(row: sqlite3.Row) -> PreferenceProfile:
    data = dict(row)
    data["property_types"] = json.loads(data["property_types"])
    data["required_features"] = json.loads(data["required_features"])
    data["excluded_features"] = json.loads(data["excluded_features"])
    data["active"] = bool(data["active"])
    data["notify_on_no_results"] = bool(data["notify_on_no_results"])
    return PreferenceProfile(**data)


class ProfileRepository:
    def __init__(self, db: Database, allowlist: AllowlistRepository, audit: AuditRepository) -> None:
        self._conn: sqlite3.Connection = db.connection
        self._allowlist = allowlist
        self._audit = audit

    def _require_allowlisted(self, telegram_user_id: int, event_type: str = "profile_access_denied") -> None:
        if not self._allowlist.is_allowlisted(telegram_user_id):
            self._audit.record(event_type, telegram_user_id, None, "unknown telegram_user_id")
            raise NotAllowlistedError(
                f"telegram_user_id {telegram_user_id} is not allowlisted"
            )

    def _fetch_row(self, profile_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM preference_profiles WHERE id = ?", (profile_id,)
        ).fetchone()

    def create_profile(
        self, telegram_user_id: int, profile: PreferenceProfileCreate
    ) -> PreferenceProfile:
        self._require_allowlisted(telegram_user_id)

        existing = self._conn.execute(
            "SELECT 1 FROM preference_profiles WHERE telegram_user_id = ? AND profile_name = ?",
            (telegram_user_id, profile.profile_name),
        ).fetchone()
        if existing is not None:
            raise DuplicateProfileNameError(
                f"user {telegram_user_id} already has a profile named {profile.profile_name!r}"
            )

        cursor = self._conn.execute(
            """
            INSERT INTO preference_profiles (
                telegram_user_id, profile_name, city, district,
                price_min, price_max, property_types,
                bed_min, bed_max, bath_min, bath_max, sqft_min, sqft_max,
                required_features, excluded_features, daily_cap, delivery_time, timezone, notify_on_no_results, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_user_id,
                profile.profile_name,
                profile.city,
                profile.district,
                profile.price_min,
                profile.price_max,
                json.dumps([pt.value for pt in profile.property_types]),
                profile.bed_min,
                profile.bed_max,
                profile.bath_min,
                profile.bath_max,
                profile.sqft_min,
                profile.sqft_max,
                json.dumps(profile.required_features),
                json.dumps(profile.excluded_features),
                profile.daily_cap,
                profile.delivery_time,
                profile.timezone,
                int(profile.notify_on_no_results),
                int(profile.active),
            ),
        )
        self._conn.commit()
        profile_id = cursor.lastrowid

        self._audit.record("profile_created", telegram_user_id, profile_id, profile.profile_name)
        return _row_to_profile(self._fetch_row(profile_id))  # type: ignore[arg-type]

    def list_profiles(self, telegram_user_id: int) -> list[PreferenceProfile]:
        self._require_allowlisted(telegram_user_id)
        rows = self._conn.execute(
            "SELECT * FROM preference_profiles WHERE telegram_user_id = ? ORDER BY id",
            (telegram_user_id,),
        ).fetchall()
        return [_row_to_profile(row) for row in rows]

    def get_profile(self, requesting_telegram_user_id: int, profile_id: int) -> PreferenceProfile:
        self._require_allowlisted(requesting_telegram_user_id)

        row = self._fetch_row(profile_id)
        if row is None:
            raise ProfileNotFoundError(f"profile {profile_id} does not exist")

        if row["telegram_user_id"] != requesting_telegram_user_id:
            self._audit.record(
                "profile_access_denied", requesting_telegram_user_id, profile_id, "cross-user access attempt"
            )
            raise ProfileAccessDeniedError(
                f"telegram_user_id {requesting_telegram_user_id} may not access profile {profile_id}"
            )

        return _row_to_profile(row)

    def update_profile(
        self, telegram_user_id: int, profile_id: int, updates: PreferenceProfileUpdate
    ) -> PreferenceProfile:
        current = self.get_profile(telegram_user_id, profile_id)

        merged_data = current.model_dump(exclude={"id", "telegram_user_id", "created_at", "updated_at"})
        update_data = updates.model_dump(exclude_unset=True)
        merged_data.update(update_data)
        validated = PreferenceProfileCreate(**merged_data)

        if validated.profile_name != current.profile_name:
            existing = self._conn.execute(
                "SELECT 1 FROM preference_profiles WHERE telegram_user_id = ? AND profile_name = ? AND id != ?",
                (telegram_user_id, validated.profile_name, profile_id),
            ).fetchone()
            if existing is not None:
                raise DuplicateProfileNameError(
                    f"user {telegram_user_id} already has a profile named {validated.profile_name!r}"
                )

        self._conn.execute(
            """
            UPDATE preference_profiles SET
                profile_name = ?, city = ?, district = ?,
                price_min = ?, price_max = ?, property_types = ?,
                bed_min = ?, bed_max = ?, bath_min = ?, bath_max = ?,
                sqft_min = ?, sqft_max = ?,
                required_features = ?, excluded_features = ?, daily_cap = ?, delivery_time = ?, timezone = ?,
                notify_on_no_results = ?, active = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (
                validated.profile_name,
                validated.city,
                validated.district,
                validated.price_min,
                validated.price_max,
                json.dumps([pt.value for pt in validated.property_types]),
                validated.bed_min,
                validated.bed_max,
                validated.bath_min,
                validated.bath_max,
                validated.sqft_min,
                validated.sqft_max,
                json.dumps(validated.required_features),
                json.dumps(validated.excluded_features),
                validated.daily_cap,
                validated.delivery_time,
                validated.timezone,
                int(validated.notify_on_no_results),
                int(validated.active),
                profile_id,
            ),
        )
        self._conn.commit()

        self._audit.record("profile_updated", telegram_user_id, profile_id, "")
        return _row_to_profile(self._fetch_row(profile_id))  # type: ignore[arg-type]

    def delete_profile(self, telegram_user_id: int, profile_id: int) -> None:
        current = self.get_profile(telegram_user_id, profile_id)

        self._conn.execute("DELETE FROM preference_profiles WHERE id = ?", (profile_id,))
        self._conn.commit()

        self._audit.record("profile_deleted", telegram_user_id, profile_id, current.profile_name)
