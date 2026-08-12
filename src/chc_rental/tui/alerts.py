"""Incremental alert operations with explicit destructive/paid confirmations."""

from __future__ import annotations

from typing import Callable, Optional

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Label

from chc_rental.tui.controller import TuiController


class ConfirmActionScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel_dialog", "Cancel")]

    def __init__(self, *, title: str, message: str, confirm_label: str) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._title)
            yield Label(self._message, id="confirm-message")
            with Horizontal():
                yield Button(
                    self._confirm_label, id="confirm-action", variant="warning"
                )
                yield Button("Cancel", id="cancel-action")

    @on(Button.Pressed, "#confirm-action")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-action")
    def _cancel(self) -> None:
        self.dismiss(False)

    def action_cancel_dialog(self) -> None:
        self.dismiss(False)


class AlertsScreen(Screen[None]):
    """Query-scope health and durable outbox actions."""

    BINDINGS = [("escape", "go_back", "Back")]

    def __init__(self, controller: TuiController) -> None:
        super().__init__()
        self._controller = controller

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("CHC Rental — Incremental Alerts")
        yield Label("", id="alerts-config")
        yield Label("", id="alerts-action")
        yield Label("Query scopes")
        yield DataTable(id="query-health-table")
        yield Label("Outbox (ambiguous sends are visible but never auto-retried)")
        yield DataTable(id="incremental-outbox-table")
        with Horizontal(classes="action-row"):
            yield Button("Pause/Resume", id="toggle-incremental")
            yield Button("Run shadow", id="run-shadow", variant="primary")
            yield Button("Retry failed", id="retry-failed")
            yield Button("Reset baseline", id="reset-baseline", variant="warning")
            yield Button("Refresh", id="refresh-alerts")
            yield Button("Back", id="back-alerts")
        yield Footer()

    def on_mount(self) -> None:
        scopes = self.query_one("#query-health-table", DataTable)
        scopes.cursor_type = "row"
        scopes.add_columns(
            "Query",
            "Source",
            "Baseline",
            "Last success",
            "Age",
            "Next due",
            "Actor run",
            "Status",
            "Results",
            "Errors",
        )
        outbox = self.query_one("#incremental-outbox-table", DataTable)
        outbox.cursor_type = "row"
        outbox.add_columns(
            "ID", "Telegram", "Search", "Event", "Status", "Due", "Attempts", "Error"
        )
        self.refresh_alerts()

    def _set_action(self, message: str) -> None:
        self.query_one("#alerts-action", Label).update(message)

    def refresh_alerts(self) -> None:
        scopes_table = self.query_one("#query-health-table", DataTable)
        outbox_table = self.query_one("#incremental-outbox-table", DataTable)
        scopes_table.clear()
        outbox_table.clear()
        try:
            status = self._controller.incremental_status()
        except Exception as exc:
            self.query_one("#alerts-config", Label).update(f"Unavailable: {exc}")
            return
        settings_state = "RUNNING" if status["enabled"] else "PAUSED"
        self.query_one("#alerts-config", Label).update(
            f"{settings_state} · Zillow {'ON' if status['zillow_enabled'] else 'OFF'} · "
            f"{'token ready' if status['token_ready'] else 'APIFY_TOKEN missing'} · "
            f"actor {status['actor']} · runs {status['used_today']}/"
            f"{status['daily_budget']} ({status['remaining_today']} left) · "
            f"interval {status['interval_minutes']}m"
        )
        health = status.get("health") or {}
        for scope in health.get("scopes", []):
            query_id = scope["query_id"]
            age = (
                "never"
                if scope["age_minutes"] is None
                else f"{scope['age_minutes']}m" + (" STALE" if scope["stale"] else "")
            )
            scopes_table.add_row(
                query_id[:12],
                scope["source"],
                scope["baseline_state"],
                scope["last_success_at"] or "never",
                age,
                scope["next_due_at"] or "not scheduled",
                scope["apify_run_id"] or "—",
                scope["run_status"] or "—",
                str(scope["result_count"] or 0)
                + (" TRUNC" if scope["truncated"] else ""),
                scope["error"] or "—",
                key=query_id,
            )
        try:
            outbox_rows = self._controller.incremental_outbox()
        except Exception as exc:
            self._set_action(f"Could not read outbox: {exc}")
            return
        for row in outbox_rows[-100:]:
            outbox_table.add_row(
                str(row.outbox_id),
                str(row.telegram_id),
                row.primary_search_name,
                row.event_type,
                row.status,
                row.not_before,
                str(row.attempts),
                row.last_error or "—",
                key=str(row.outbox_id),
            )

    @staticmethod
    def _selected_key(table: DataTable) -> Optional[str]:
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value)

    @on(Button.Pressed, "#toggle-incremental")
    def _toggle_incremental(self) -> None:
        try:
            current = self._controller.settings().incremental_alerts_enabled
            saved = self._controller.set_incremental_enabled(not current)
        except Exception as exc:
            self._set_action(f"State unchanged: {exc}")
            return
        self._set_action(
            "Incremental collection resumed."
            if saved.incremental_alerts_enabled
            else "Incremental collection paused; no new Apify starts will occur."
        )
        self.refresh_alerts()

    @on(Button.Pressed, "#run-shadow")
    def _run_shadow(self) -> None:
        def handle(confirmed: bool) -> None:
            if not confirmed:
                return
            self._set_action("Starting one source cycle; Telegram delivery remains disabled…")
            self._run_shadow_worker()

        self.app.push_screen(
            ConfirmActionScreen(
                title="Run one paid shadow cycle?",
                message=(
                    "This may start one bounded Apify actor run and consume the daily/cost "
                    "ceiling. It stages shadow rows only and cannot send Telegram."
                ),
                confirm_label="Run shadow",
            ),
            handle,
        )

    @work(thread=True, exclusive=True, group="shadow-cycle", exit_on_error=False)
    def _run_shadow_worker(self) -> None:
        try:
            summary = self._controller.run_shadow_cycle()
            message = (
                f"Shadow cycle complete: {summary['started']} started, "
                f"{summary['resumed']} resumed, {summary['records']} records, "
                f"{summary['processing']['outbox']['queued']} shadow alerts."
            )
        except Exception as exc:
            message = f"Shadow cycle failed: {exc}"
        self.app.call_from_thread(self._finish_shadow, message)

    def _finish_shadow(self, message: str) -> None:
        self._set_action(message)
        self.refresh_alerts()

    @on(Button.Pressed, "#retry-failed")
    def _retry_failed(self) -> None:
        selected = self._selected_key(
            self.query_one("#incremental-outbox-table", DataTable)
        )
        if selected is None:
            self._set_action("Select an outbox item first.")
            return
        try:
            self._controller.retry_failed_outbox(int(selected))
        except Exception as exc:
            self._set_action(f"Not retried: {exc}")
            return
        self._set_action(f"Outbox {selected} is waiting for a controlled retry.")
        self.refresh_alerts()

    @on(Button.Pressed, "#reset-baseline")
    def _reset_baseline(self) -> None:
        query_id = self._selected_key(self.query_one("#query-health-table", DataTable))
        if query_id is None:
            self._set_action("Select a query scope first.")
            return

        def handle(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                self._controller.reset_baseline(
                    query_id, reason="confirmed in owner dashboard"
                )
            except Exception as exc:
                self._set_action(f"Baseline unchanged: {exc}")
                return
            self._set_action(
                "Baseline reset recorded; the next successful result window will be silent."
            )
            self.refresh_alerts()

        self.app.push_screen(
            ConfirmActionScreen(
                title="Reset this query baseline?",
                message=(
                    "The next successful window becomes a silent baseline. The action is "
                    "written to the operator audit ledger."
                ),
                confirm_label="Reset baseline",
            ),
            handle,
        )

    @on(Button.Pressed, "#refresh-alerts")
    def _refresh(self) -> None:
        self.refresh_alerts()

    @on(Button.Pressed, "#back-alerts")
    def _back(self) -> None:
        self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()
