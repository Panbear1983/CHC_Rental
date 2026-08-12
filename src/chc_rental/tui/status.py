"""Operator status and guarded incremental-source controls.

Shows the allowlist rollup, daily and incremental source health, outbox state,
and the last run. The one configuration action writes through TuiController.

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
from chc_rental.tui.alert_settings import IncrementalSettingsScreen


class StatusScreen(Screen[None]):
    """Operational snapshot plus a real incremental-settings control."""

    BINDINGS = [("escape", "go_back", "Back")]

    def __init__(self, controller: TuiController) -> None:
        super().__init__()
        self._controller = controller

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("CHC Rental — Status")
        yield Label("", id="people-summary")
        yield Label("", id="quota-summary")
        yield Label("", id="run-summary")
        yield Label("", id="push-summary")
        yield Label("Daily listing sources")
        yield DataTable(id="source-status-table")
        yield Label("Incremental Zillow source")
        yield Label("", id="incremental-config-summary")
        yield Label("", id="incremental-run-summary")
        yield Label("", id="incremental-outbox-summary")
        yield Label("", id="incremental-action")
        yield Label("Searches by person")
        yield DataTable(id="search-summary-table")
        with Horizontal(classes="action-row"):
            yield Button("Refresh", id="refresh")
            yield Button("Alert settings", id="alert-settings")
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        sources = self.query_one("#source-status-table", DataTable)
        sources.cursor_type = "row"
        sources.add_columns("Source", "Config", "Requests", "Cache", "Records", "Last result")
        table = self.query_one("#search-summary-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Telegram ID",
            "Person",
            "Allowlisted",
            "Active searches",
            "Delivery",
            "Alert outbox",
        )
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
        except Exception as exc:
            self.query_one("#quota-summary", Label).update(f"Could not read settings: {exc}")
        else:
            mode = "LIVE PUSH ENABLED" if settings.live_push_enabled else "dry-run only"
            self.query_one("#quota-summary", Label).update(
                f"Requests today: {used}/{limit} · rejected records today: {rejected} · {mode}"
            )

        source_table = self.query_one("#source-status-table", DataTable)
        source_table.clear()
        try:
            source_rows = self._controller.source_status_today()
        except Exception as exc:
            source_table.add_row("—", f"unavailable: {exc}", "—", "—", "—", "error")
        else:
            for row in source_rows:
                source_table.add_row(
                    row["source"],
                    row["readiness"],
                    f"{row['used']}/{row['budget']}",
                    "today" if row["cached"] else "none",
                    str(row["records"]),
                    row["health"],
                    key=row["source"],
                )

        self._refresh_incremental()

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
            try:
                outbox = self._controller.recipient_outbox_counts(person.telegram_id)
            except Exception:
                outbox = {}
            pending = outbox.get("pending", 0) + outbox.get("retry_wait", 0)
            failed = outbox.get("failed", 0) + outbox.get("uncertain", 0)
            shadow = outbox.get("shadow", 0)
            if profile.delivery_mode.value == "immediate":
                delivery = "immediate"
                if profile.quiet_hours_start:
                    delivery += (
                        f" · quiet {profile.quiet_hours_start}-{profile.quiet_hours_end}"
                    )
            else:
                delivery = f"daily {profile.delivery_time} {profile.timezone}"
            table.add_row(
                str(person.telegram_id),
                person.display_name,
                "yes" if person.active else "no",
                f"{len(profile.active_searches())}/{len(profile.searches)}",
                delivery,
                f"shadow {shadow} · pending {pending} · failed {failed}",
                key=str(person.telegram_id),
            )

    def _refresh_incremental(self) -> None:
        try:
            status = self._controller.incremental_status()
        except Exception as exc:
            self.query_one("#incremental-config-summary", Label).update(
                f"Unavailable: {exc}"
            )
            self.query_one("#incremental-run-summary", Label).update("")
            self.query_one("#incremental-outbox-summary", Label).update("")
            return
        config_ready = "ready" if status["config"]["ready"] else "migration required"
        ledger_ready = "ready" if status["ledger"]["ready"] else "migration required"
        token = "token ready" if status["token_ready"] else "APIFY_TOKEN missing"
        gate = "ON" if status["enabled"] else "OFF"
        source = "ON" if status["zillow_enabled"] else "OFF"
        self.query_one("#incremental-config-summary", Label).update(
            f"Global {gate} · Zillow {source} · {token} · config {config_ready} · "
            f"ledger {ledger_ready} · actor {status['actor']} · every "
            f"{status['interval_minutes']}m · runs {status['used_today']}/"
            f"{status['daily_budget']} ({status['remaining_today']} left) · "
            f"{status['results_limit']} rows/run · "
            f"${status['max_charge_usd']:.2f}/run max · {status['active_window']} · "
            f"canaries {status['canary_ids'] or 'none'}"
        )
        health = status.get("health")
        if not health:
            self.query_one("#incremental-run-summary", Label).update(
                "Run health unavailable until the alert-ledger migration is applied."
            )
            self.query_one("#incremental-outbox-summary", Label).update("")
            return
        staleness = "STALE" if health.get("stale") else "current"
        truncated = " · TRUNCATED" if health.get("last_truncated") else ""
        error = f" · error {health['last_error']}" if health.get("last_error") else ""
        self.query_one("#incremental-run-summary", Label).update(
            f"Scopes {health['active_scopes']} · baselines pending "
            f"{health['pending_baselines']} · running {health['running_runs']} · "
            f"next {health['next_due_at'] or 'not scheduled'} · last attempt "
            f"{health['last_attempt_at'] or 'never'} ({health['last_status'] or 'none'}) · "
            f"last success {health['last_success_at'] or 'never'} [{staleness}] · "
            f"results {health['last_result_count'] or 0}{truncated}{error}"
        )
        outbox = health["outbox"]
        self.query_one("#incremental-outbox-summary", Label).update(
            f"Outbox: shadow {outbox.get('shadow', 0)} · pending "
            f"{outbox.get('pending', 0)} · retry {outbox.get('retry_wait', 0)} · "
            f"failed {outbox.get('failed', 0)} · uncertain "
            f"{outbox.get('uncertain', 0)} · sent {outbox.get('sent', 0)} · "
            f"known source cost ${health['known_cost_usd']:.2f} · "
            f"unknown-charge runs {health['unknown_charges']}"
        )

    @on(Button.Pressed, "#refresh")
    def _refresh(self) -> None:
        self.refresh_status()

    @on(Button.Pressed, "#alert-settings")
    def _alert_settings(self) -> None:
        try:
            settings = self._controller.settings()
        except Exception as exc:
            self.query_one("#incremental-action", Label).update(str(exc))
            return

        def handle(result: dict | None) -> None:
            if result is None:
                return
            try:
                self._controller.update_incremental_settings(**result)
            except Exception as exc:
                self.query_one("#incremental-action", Label).update(
                    f"Settings were not changed: {exc}"
                )
                return
            self.query_one("#incremental-action", Label).update(
                "Incremental settings saved; no scrape was started."
            )
            self.refresh_status()

        self.app.push_screen(IncrementalSettingsScreen(settings=settings), handle)

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()
