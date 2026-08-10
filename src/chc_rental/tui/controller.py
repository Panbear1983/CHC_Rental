"""Service layer the TUI screens call into.

Every mutation goes through `Store.edit_allowlist`, which holds one lock across
the whole read-modify-write. No screen may touch a file directly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from chc_rental.models import AllowlistEntry, Profile, Search, Settings
from chc_rental.pipeline import PipelineResult, plan_pushes
from chc_rental.store import Store, StoreError
from chc_rental.tui.forms import parse_search_form


class DuplicateError(ValueError):
    """Raised when a telegram id or search name is already taken."""


class NotFoundError(ValueError):
    """Raised when a person or search does not exist."""


class TuiController:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ people
    def list_people(self) -> list[AllowlistEntry]:
        return self.store.load_allowlist().people

    def get_person(self, telegram_id: int) -> AllowlistEntry:
        person = self.store.load_allowlist().get(telegram_id)
        if person is None:
            raise NotFoundError(f"no allowlisted person with id {telegram_id}")
        return person

    def add_person(self, telegram_id: int, display_name: str) -> None:
        with self.store.edit_allowlist() as allowlist:
            if allowlist.get(telegram_id) is not None:
                raise DuplicateError(f"telegram id {telegram_id} is already on the allowlist")
            allowlist.people.append(
                AllowlistEntry(
                    telegram_id=telegram_id, display_name=display_name, profile=Profile()
                )
            )

    def set_person_active(self, telegram_id: int, active: bool) -> None:
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            person.active = active

    def remove_person(self, telegram_id: int) -> None:
        with self.store.edit_allowlist() as allowlist:
            if allowlist.get(telegram_id) is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            allowlist.people = [p for p in allowlist.people if p.telegram_id != telegram_id]

    # ---------------------------------------------------------------- searches
    def list_searches(self, telegram_id: int) -> list[Search]:
        return self.get_person(telegram_id).profile.searches

    def add_search(self, telegram_id: int, form_data: dict) -> None:
        search = parse_search_form(form_data)
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            existing = {s.name.lower() for s in person.profile.searches}
            if search.name.lower() in existing:
                raise DuplicateError(f"this profile already has a search named {search.name!r}")
            person.profile.searches.append(search)

    def update_search(self, telegram_id: int, index: int, form_data: dict) -> None:
        search = parse_search_form(form_data)
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            if not 0 <= index < len(person.profile.searches):
                raise NotFoundError(f"no search at position {index}")
            clashes = {
                s.name.lower()
                for position, s in enumerate(person.profile.searches)
                if position != index
            }
            if search.name.lower() in clashes:
                raise DuplicateError(f"this profile already has a search named {search.name!r}")
            person.profile.searches[index] = search

    def delete_search(self, telegram_id: int, index: int) -> None:
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            if not 0 <= index < len(person.profile.searches):
                raise NotFoundError(f"no search at position {index}")
            person.profile.searches.pop(index)

    def toggle_search(self, telegram_id: int, index: int) -> None:
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            if not 0 <= index < len(person.profile.searches):
                raise NotFoundError(f"no search at position {index}")
            search = person.profile.searches[index]
            search.active = not search.active

    # ----------------------------------------------------------------- profile
    def update_delivery(
        self,
        telegram_id: int,
        *,
        delivery_time: str,
        timezone_name: str,
        notify_on_no_results: bool,
    ) -> None:
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            person.profile.delivery_time = delivery_time
            person.profile.timezone = timezone_name
            person.profile.notify_on_no_results = notify_on_no_results

    # ------------------------------------------------------------------ status
    def settings(self) -> Settings:
        return self.store.load_settings()

    def latest_run(self) -> Optional[dict]:
        return self.store.latest_run_log()

    def quota_today(self) -> tuple[int, int]:
        settings = self.store.load_settings()
        used = self.store.quota_total(date.today())
        return used, settings.global_daily_request_budget

    def rejected_today(self) -> int:
        return self.store.rejected_count(date.today())

    def preview(self, listings, *, now_utc: Optional[datetime] = None) -> PipelineResult:
        return plan_pushes(
            self.store, listings, now_utc=now_utc or datetime.now(timezone.utc)
        )


__all__ = ["TuiController", "DuplicateError", "NotFoundError", "StoreError"]
