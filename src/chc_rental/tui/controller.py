"""Service layer the TUI screens call into.

Every mutation goes through `Store.edit_allowlist`, which holds one lock across
the whole read-modify-write. No screen may touch a file directly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from uuid import uuid4

from chc_rental.models import AllowlistEntry, Profile, Search, Settings
from chc_rental.pipeline import PipelineResult, plan_pushes
from chc_rental.sources import KNOWN_SOURCES
from chc_rental.sources.rentcast import load_rentcast_key
from chc_rental.sources.zillow import load_apify_token
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
        search.search_id = str(uuid4())
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
            search.search_id = person.profile.searches[index].search_id
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

    @staticmethod
    def _today() -> date:
        # The runner keys quota/rejected files by the UTC date, so the status
        # screen must look up the same day — local date.today() diverges from
        # it every evening west of Greenwich.
        return datetime.now(timezone.utc).date()

    def quota_today(self) -> tuple[int, int]:
        settings = self.store.load_settings()
        used = self.store.quota_total(self._today())
        return used, settings.global_daily_request_budget

    def source_status_today(self) -> list[dict]:
        """Return real per-source config, quota, cache, and last-run state."""
        today = self._today()
        settings = self.store.load_settings()
        env_path = str(self.store.root / ".env")
        credentials = {
            "rentcast": load_rentcast_key(env_path) is not None,
            "zillow": load_apify_token(env_path) is not None,
        }
        latest = self.store.latest_run_log() or {}
        fetch = latest.get("fetch") if isinstance(latest, dict) else {}
        source_runs = fetch.get("sources") if isinstance(fetch, dict) else None
        if not isinstance(source_runs, list):
            source_runs = [fetch] if isinstance(fetch, dict) and fetch.get("source") else []
        by_source = {
            str(item.get("source")): item
            for item in source_runs
            if isinstance(item, dict) and item.get("source")
        }

        rows: list[dict] = []
        for source in KNOWN_SOURCES:
            budget = settings.source_request_budget(source)
            enabled = credentials[source] and budget > 0
            readiness = "ready" if enabled else "disabled"
            if source == "zillow":
                enabled = settings.zillow_enabled and credentials[source] and budget > 0
                if not settings.zillow_enabled:
                    readiness = "disabled"
                elif not credentials[source]:
                    readiness = "needs token"
                elif budget <= 0 or settings.zillow_results_limit <= 0:
                    readiness = "budget 0"
                else:
                    readiness = "ready"
            elif not credentials[source]:
                readiness = "needs key"
            elif budget <= 0:
                readiness = "budget 0"

            run = by_source.get(source) or {}
            if run.get("errors"):
                health = "error"
            elif run.get("from_cache"):
                health = "cached"
            elif run.get("fetched"):
                health = "ok"
            elif run.get("warnings"):
                health = "waiting"
            else:
                health = "not run"
            rows.append(
                {
                    "source": source,
                    "enabled": enabled,
                    "readiness": readiness,
                    "used": self.store.quota_used(today, source),
                    "budget": budget,
                    "cached": self.store.cache_path(today, source).is_file(),
                    "records": run.get("records", 0),
                    "health": health,
                }
            )
        return rows

    def rejected_today(self) -> int:
        return self.store.rejected_count(self._today())

    def preview(self, listings, *, now_utc: Optional[datetime] = None) -> PipelineResult:
        return plan_pushes(
            self.store, listings, now_utc=now_utc or datetime.now(timezone.utc)
        )


__all__ = ["TuiController", "DuplicateError", "NotFoundError", "StoreError"]
