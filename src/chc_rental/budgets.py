"""Persistent global daily source/candidate budget ledger with a circuit
breaker.

One row per calendar `budget_date`. `consume` is the only way to spend
budget: it never allows `allocated` to exceed `daily_limit` (so remaining
budget never goes negative), and it trips the circuit breaker exactly when
the limit is reached. Once tripped, further consumption for that date is
rejected until a new `budget_date` row exists (a new day).

No network or scheduling logic lives here: this module only reads and
writes local SQLite state.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from chc_rental.db import Database
from chc_rental.errors import BudgetExhaustedError
from chc_rental.models import BudgetState


def _row_to_state(row: sqlite3.Row) -> BudgetState:
    return BudgetState(
        budget_date=row["budget_date"],
        daily_limit=row["daily_limit"],
        allocated=row["allocated"],
        circuit_breaker_tripped=bool(row["circuit_breaker_tripped"]),
    )


class BudgetRepository:
    def __init__(self, db: Database) -> None:
        self._conn: sqlite3.Connection = db.connection

    def _fetch(self, budget_date: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM daily_budget_ledger WHERE budget_date = ?", (budget_date,)
        ).fetchone()

    def get_state(self, budget_date: str) -> Optional[BudgetState]:
        row = self._fetch(budget_date)
        return _row_to_state(row) if row is not None else None

    def get_or_create(self, budget_date: str, daily_limit: int) -> BudgetState:
        if daily_limit < 0:
            raise ValueError("daily_limit must be non-negative")
        row = self._fetch(budget_date)
        if row is not None:
            return _row_to_state(row)
        self._conn.execute(
            "INSERT INTO daily_budget_ledger (budget_date, daily_limit, allocated, circuit_breaker_tripped) "
            "VALUES (?, ?, 0, 0)",
            (budget_date, daily_limit),
        )
        self._conn.commit()
        return _row_to_state(self._fetch(budget_date))  # type: ignore[arg-type]

    def consume(self, budget_date: str, amount: int) -> BudgetState:
        if amount <= 0:
            raise ValueError("amount must be a positive integer")
        row = self._fetch(budget_date)
        if row is None:
            raise LookupError(f"no budget ledger row for {budget_date!r}; call get_or_create first")
        if row["circuit_breaker_tripped"]:
            raise BudgetExhaustedError(f"circuit breaker already tripped for {budget_date}")
        remaining = row["daily_limit"] - row["allocated"]
        if amount > remaining:
            raise BudgetExhaustedError(
                f"requested {amount} exceeds remaining budget {remaining} for {budget_date}"
            )
        new_allocated = row["allocated"] + amount
        tripped = 1 if new_allocated >= row["daily_limit"] else 0
        self._conn.execute(
            "UPDATE daily_budget_ledger SET allocated = ?, circuit_breaker_tripped = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE budget_date = ?",
            (new_allocated, tripped, budget_date),
        )
        self._conn.commit()
        return _row_to_state(self._fetch(budget_date))  # type: ignore[arg-type]

    def trip_circuit_breaker(self, budget_date: str) -> BudgetState:
        row = self._fetch(budget_date)
        if row is None:
            raise LookupError(f"no budget ledger row for {budget_date!r}; call get_or_create first")
        self._conn.execute(
            "UPDATE daily_budget_ledger SET circuit_breaker_tripped = 1, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE budget_date = ?",
            (budget_date,),
        )
        self._conn.commit()
        return _row_to_state(self._fetch(budget_date))  # type: ignore[arg-type]
