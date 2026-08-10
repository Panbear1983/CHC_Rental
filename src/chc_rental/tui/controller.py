"""Service layer the TUI screens call into.

No business validation lives here: form coercion happens in `chc_rental.tui.forms`
and every rule about ranges, allowlist membership, cross-user access, and
duplicate names is enforced by the existing repositories and Pydantic models.
"""

from __future__ import annotations

from chc_rental.budgets import BudgetRepository
from chc_rental.db import Database
from chc_rental.delivery import DeliveryRepository
from chc_rental.models import AllowlistedUser, PreferenceProfile
from chc_rental.repositories import AllowlistRepository, AuditRepository, ProfileRepository
from chc_rental.status import StatusSnapshot, build_status_snapshot
from chc_rental.tui.forms import parse_profile_create_form, parse_profile_update_form

DEFAULT_DAILY_BUDGET_LIMIT = 100


class TuiController:
    def __init__(self, db: Database, *, daily_budget_limit: int = DEFAULT_DAILY_BUDGET_LIMIT) -> None:
        self.allowlist = AllowlistRepository(db)
        self.audit = AuditRepository(db)
        self.profiles = ProfileRepository(db, self.allowlist, self.audit)
        self.budgets = BudgetRepository(db)
        self.delivery = DeliveryRepository(db)
        self._daily_budget_limit = daily_budget_limit

    def list_users(self) -> list[AllowlistedUser]:
        return self.allowlist.list_users()

    def add_user(self, telegram_user_id: int, display_name: str) -> AllowlistedUser:
        return self.allowlist.add_user(telegram_user_id, display_name)

    def deactivate_user(self, telegram_user_id: int) -> AllowlistedUser:
        return self.allowlist.deactivate_user(telegram_user_id)

    def list_profiles(self, telegram_user_id: int) -> list[PreferenceProfile]:
        return self.profiles.list_profiles(telegram_user_id)

    def get_profile(self, telegram_user_id: int, profile_id: int) -> PreferenceProfile:
        return self.profiles.get_profile(telegram_user_id, profile_id)

    def create_profile(self, telegram_user_id: int, form_data: dict) -> PreferenceProfile:
        profile = parse_profile_create_form(form_data)
        return self.profiles.create_profile(telegram_user_id, profile)

    def update_profile(
        self, telegram_user_id: int, profile_id: int, form_data: dict
    ) -> PreferenceProfile:
        updates = parse_profile_update_form(form_data)
        return self.profiles.update_profile(telegram_user_id, profile_id, updates)

    def delete_profile(self, telegram_user_id: int, profile_id: int) -> None:
        self.profiles.delete_profile(telegram_user_id, profile_id)

    def get_status_snapshot(self, budget_date: str) -> StatusSnapshot:
        return build_status_snapshot(
            self.allowlist,
            self.profiles,
            self.budgets,
            self.delivery,
            budget_date,
            self._daily_budget_limit,
        )
