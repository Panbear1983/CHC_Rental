"""Read-only operator status screen.

Shows the allowlist rollup, today's request quota, the last run, and how many
records were rejected. Nothing here mutates anything.

Every value is fetched defensively: this screen previously crashed the whole
dashboard because it walked the allowlist through a call that raised for anyone
who had been deactivated. A status view must never be able to kill the app.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Label

from chc_rental.tui.controller import TuiController


class StatusScreen(Screen[None]):
    """Read-only snapshot; no create/edit/delete actions live here."""

    BINDINGS = [("escape", "go_back", "Back")]

    def __init__(self, controller: TuiController) -> None:
        super().__init__()
        self._controller = controller

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("CHC Rental — Status (read-only)")
        yield Label("", id="people-summary")
        yield Label("", id="quota-summary")
        yield Label("", id="run-summary")
        yield Label("", id="push-summary")
        yield Label("Searches by person")
        yield DataTable(id="search-summary-table")
        with Horizontal(classes="action-row"):
            yield Button("Refresh", id="refresh")
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#search-summary-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Telegram ID", "Person", "Allowlisted", "Active searches", "Delivery")
        self.refresh_status()

    def refresh_status(self) -> None:
        try:
            people = self._controller.list_people()
        except Exception as exc:  # a status view must never crash the dashboard
            self.query_one("#people-summary", Label).update(f"Could not read allowlist: {exc}")
            return

        allowlisted = [person for person in people if person.active]
        self.query_one("#people-summary", Label).update(
            f"People: {len(allowlisted)} allowlisted / {len(people)} total"
        )

        try:
            used, limit = self._controller.quota_today()
            rejected = self._controller.rejected_today()
            settings = self._controller.settings()
            mode = "LIVE PUSH ENABLED" if settings.live_push_enabled else "dry-run only"
        except Exception as exc:
            self.query_one("#quota-summary", Label).update(f"Could not read settings: {exc}")
            used = limit = rejected = 0
            mode = "unknown"
        self.query_one("#quota-summary", Label).update(
            f"Requests today: {used}/{limit} · rejected records today: {rejected} · {mode}"
        )

        run = None
        try:
            run = self._controller.latest_run()
        except Exception:
            run = None
        if run is None:
            self.query_one("#run-summary", Label).update("Last run: none recorded")
            self.query_one("#push-summary", Label).update("")
        else:
            summary = run.get("summary", {}) if isinstance(run, dict) else {}
            self.query_one("#run-summary", Label).update(
                f"Last run: {run.get('finished_at', 'unknown')} · mode {summary.get('delivery', '?')}"
            )
            self.query_one("#push-summary", Label).update(
                f"Planned {summary.get('planned_pushes', 0)} · sent {summary.get('sent', 0)} · "
                f"failed {summary.get('failed', 0)} · due {summary.get('people_due', 0)}"
            )

        table = self.query_one("#search-summary-table", DataTable)
        table.clear()
        for person in people:
            profile = person.profile
            table.add_row(
                str(person.telegram_id),
                person.display_name,
                "yes" if person.active else "no",
                f"{len(profile.active_searches())}/{len(profile.searches)}",
                f"{profile.delivery_time} {profile.timezone}",
                key=str(person.telegram_id),
            )

    @on(Button.Pressed, "#refresh")
    def _refresh(self) -> None:
        self.refresh_status()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()
