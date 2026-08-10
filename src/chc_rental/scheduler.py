"""Deterministic, equal round-robin allocation for offline candidate queues."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProfileCandidates:
    profile_id: int
    daily_cap: int
    candidates: list[Any]


def plan_daily_allocation(profiles: list[ProfileCandidates], *, global_daily_budget: int) -> dict[int, list[Any]]:
    if global_daily_budget < 0:
        raise ValueError("global_daily_budget must be non-negative")
    ordered = sorted(profiles, key=lambda item: item.profile_id)
    if len({item.profile_id for item in ordered}) != len(ordered):
        raise ValueError("profile IDs must be unique")
    allocation = {item.profile_id: [] for item in ordered}
    cursors = {item.profile_id: 0 for item in ordered}
    remaining = global_daily_budget
    while remaining:
        progressed = False
        for item in ordered:
            selected = allocation[item.profile_id]
            cursor = cursors[item.profile_id]
            if len(selected) >= item.daily_cap or cursor >= len(item.candidates):
                continue
            selected.append(item.candidates[cursor])
            cursors[item.profile_id] = cursor + 1
            remaining -= 1
            progressed = True
            if not remaining:
                break
        if not progressed:
            break
    return allocation
