"""Read-only aggregation of allowlist, profile, budget, and delivery state
for the operator status view.

Pure aggregation over existing repositories: no network calls, and no
mutation beyond `BudgetRepository.get_or_create`'s idempotent row creation
(the same lazy-init already used elsewhere in this codebase).
"""

from __future__ import annotations

from dataclasses import dataclass

from chc_rental.budgets import BudgetRepository
from chc_rental.delivery import DeliveryRepository
from chc_rental.models import BudgetState
from chc_rental.repositories import AllowlistRepository, ProfileRepository


@dataclass(frozen=True)
class ProfileCapInfo:
    telegram_user_id: int
    display_name: str
    profile_id: int
    profile_name: str
    daily_cap: int
    active: bool


@dataclass(frozen=True)
class DeliveryCounts:
    pending: int
    sent: int
    failed: int


@dataclass(frozen=True)
class StatusSnapshot:
    active_user_count: int
    total_user_count: int
    profile_caps: list[ProfileCapInfo]
    budget: BudgetState
    delivery_counts: DeliveryCounts


def build_status_snapshot(
    allowlist: AllowlistRepository,
    profiles: ProfileRepository,
    budgets: BudgetRepository,
    delivery: DeliveryRepository,
    budget_date: str,
    daily_budget_limit: int,
) -> StatusSnapshot:
    users = allowlist.list_users()

    profile_caps = [
        ProfileCapInfo(
            telegram_user_id=user.telegram_user_id,
            display_name=user.display_name,
            profile_id=profile.id,
            profile_name=profile.profile_name,
            daily_cap=profile.daily_cap,
            active=profile.active,
        )
        for user in users
        for profile in profiles.list_profiles(user.telegram_user_id)
    ]

    budget_state = budgets.get_or_create(budget_date, daily_budget_limit)

    raw_counts = delivery.counts()
    delivery_counts = DeliveryCounts(
        pending=raw_counts.get("pending", 0),
        sent=raw_counts.get("sent", 0),
        failed=raw_counts.get("failed", 0),
    )

    return StatusSnapshot(
        active_user_count=sum(1 for user in users if user.active),
        total_user_count=len(users),
        profile_caps=profile_caps,
        budget=budget_state,
        delivery_counts=delivery_counts,
    )
