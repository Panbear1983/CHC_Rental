"""Read-only operator status screen.

Shows active users/profiles, per-profile daily caps, the current global
daily budget/circuit-breaker state, and delivery counts. No inputs mutate
anything here: this screen only reads `TuiController.get_status_snapshot`.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Label

from chc_rental.tui.controller import TuiController


class StatusScreen(Screen[None]):
    """Read-only snapshot view: no create/edit/delete actions live here."""

    BINDINGS = [("escape", "go_back", "Back")]

    def __init__(self, controller: TuiController, budget_date: str) -> None:
        super().__init__()
        self._controller = controller
        self._budget_date = budget_date

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("CHC Rental — Status (read-only)")
        yield Label("", id="user-summary")
        yield Label("", id="budget-summary")
        yield Label("", id="delivery-summary")
        yield Label("Profile caps")
        yield DataTable(id="profile-caps-table")
        yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#profile-caps-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Telegram ID", "User", "Profile", "Daily cap", "Active")
        self.refresh_status()

    def refresh_status(self) -> None:
        snapshot = self._controller.get_status_snapshot(self._budget_date)

        self.query_one("#user-summary", Label).update(
            f"Users: {snapshot.active_user_count} active / {snapshot.total_user_count} total"
        )

        budget = snapshot.budget
        state = "CIRCUIT BREAKER TRIPPED" if budget.circuit_breaker_tripped else "ok"
        self.query_one("#budget-summary", Label).update(
            f"Budget {self._budget_date}: allocated {budget.allocated}/{budget.daily_limit} "
            f"(remaining {budget.remaining}) — {state}"
        )

        counts = snapshot.delivery_counts
        self.query_one("#delivery-summary", Label).update(
            f"Deliveries: pending {counts.pending} · sent {counts.sent} · failed {counts.failed}"
        )

        table = self.query_one("#profile-caps-table", DataTable)
        table.clear()
        for cap in snapshot.profile_caps:
            table.add_row(
                str(cap.telegram_user_id),
                cap.display_name,
                cap.profile_name,
                str(cap.daily_cap),
                "yes" if cap.active else "no",
                key=str(cap.profile_id),
            )

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()
