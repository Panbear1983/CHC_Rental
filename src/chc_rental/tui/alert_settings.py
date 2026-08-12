"""Owner-facing controls for the real incremental source configuration."""

from __future__ import annotations

from typing import Optional

from pydantic import ValidationError
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Switch

from chc_rental.models import Settings


class IncrementalSettingsScreen(ModalScreen[Optional[dict]]):
    """Edit guarded Zillow collection ceilings without starting a scrape."""

    BINDINGS = [("escape", "cancel_dialog", "Cancel")]

    def __init__(self, *, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    def compose(self) -> ComposeResult:
        settings = self._settings
        with Vertical(id="dialog"):
            yield Label("Incremental Zillow alert settings")
            yield Label("", id="incremental-form-error")
            with VerticalScroll(classes="dialog-fields"):
                with Horizontal():
                    yield Label("Incremental collection enabled")
                    yield Switch(
                        value=settings.incremental_alerts_enabled,
                        id="incremental_alerts_enabled",
                    )
                with Horizontal():
                    yield Label("Zillow source enabled")
                    yield Switch(value=settings.zillow_enabled, id="zillow_enabled")
                yield Label(
                    "Zillow is a third-party managed scraper. Confirm you accept its "
                    "terms/cost responsibility before first enabling it."
                )
                with Horizontal():
                    yield Label("I confirm the Zillow scraper warning")
                    yield Switch(value=settings.zillow_enabled, id="zillow_terms_confirmed")
                yield Label("Apify Actor ID")
                yield Input(value=settings.zillow_actor, id="zillow_actor")
                yield Label("Collection interval in minutes")
                yield Input(
                    value=str(settings.zillow_incremental_interval_minutes),
                    id="zillow_interval",
                )
                yield Label("Zillow paid starts per UTC day")
                yield Input(
                    value=str(settings.source_request_budget("zillow")),
                    id="zillow_daily_budget",
                )
                yield Label("Maximum dataset rows per run")
                yield Input(value=str(settings.zillow_results_limit), id="zillow_results_limit")
                yield Label("Maximum Apify charge per run (USD)")
                yield Input(
                    value=str(settings.zillow_max_charge_usd), id="zillow_max_charge_usd"
                )
                yield Label("Monthly incremental soft budget (USD; blank = unset)")
                yield Input(
                    value=(
                        ""
                        if settings.incremental_monthly_budget_usd is None
                        else str(settings.incremental_monthly_budget_usd)
                    ),
                    id="incremental_monthly_budget_usd",
                )
                yield Label("Active-window start (HH:MM)")
                yield Input(
                    value=settings.incremental_active_start,
                    id="incremental_active_start",
                )
                yield Label("Active-window end (HH:MM)")
                yield Input(
                    value=settings.incremental_active_end,
                    id="incremental_active_end",
                )
                yield Label("Live-delivery canary Telegram IDs (comma-separated)")
                yield Input(
                    value=", ".join(map(str, settings.incremental_canary_telegram_ids)),
                    id="incremental_canary_telegram_ids",
                )
            with Horizontal():
                yield Button("Save", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._opened_with = self._collect()
        self._discard_armed = False
        self.query_one("#zillow_interval", Input).focus()

    def _collect(self) -> dict:
        return {
            "incremental_alerts_enabled": self.query_one(
                "#incremental_alerts_enabled", Switch
            ).value,
            "zillow_enabled": self.query_one("#zillow_enabled", Switch).value,
            "zillow_terms_confirmed": self.query_one(
                "#zillow_terms_confirmed", Switch
            ).value,
            "zillow_actor": self.query_one("#zillow_actor", Input).value,
            "zillow_interval": self.query_one("#zillow_interval", Input).value,
            "zillow_daily_budget": self.query_one(
                "#zillow_daily_budget", Input
            ).value,
            "zillow_results_limit": self.query_one(
                "#zillow_results_limit", Input
            ).value,
            "zillow_max_charge_usd": self.query_one(
                "#zillow_max_charge_usd", Input
            ).value,
            "incremental_monthly_budget_usd": self.query_one(
                "#incremental_monthly_budget_usd", Input
            ).value,
            "incremental_active_start": self.query_one(
                "#incremental_active_start", Input
            ).value,
            "incremental_active_end": self.query_one(
                "#incremental_active_end", Input
            ).value,
            "incremental_canary_telegram_ids": self.query_one(
                "#incremental_canary_telegram_ids", Input
            ).value,
        }

    def _validated(self) -> dict:
        raw = self._collect()
        try:
            interval = int(raw["zillow_interval"].strip())
            daily_budget = int(raw["zillow_daily_budget"].strip())
            results_limit = int(raw["zillow_results_limit"].strip())
            max_charge = float(raw["zillow_max_charge_usd"].strip())
            monthly_text = raw["incremental_monthly_budget_usd"].strip()
            monthly_budget = float(monthly_text) if monthly_text else None
            canary_ids = [
                int(item.strip())
                for item in raw["incremental_canary_telegram_ids"].split(",")
                if item.strip()
            ]
        except ValueError as exc:
            raise ValueError("interval, limits, and costs must be valid numbers") from exc
        if raw["zillow_enabled"] and not self._settings.zillow_enabled:
            if not raw["zillow_terms_confirmed"]:
                raise ValueError("confirm the Zillow scraper warning before enabling it")
        if raw["incremental_alerts_enabled"] and not raw["zillow_enabled"]:
            raise ValueError("enable Zillow before enabling incremental collection")
        source_budgets = dict(self._settings.source_daily_request_budgets)
        source_budgets["zillow"] = daily_budget
        candidate = Settings.model_validate(
            {
                **self._settings.model_dump(mode="python"),
                "incremental_alerts_enabled": raw["incremental_alerts_enabled"],
                "zillow_enabled": raw["zillow_enabled"],
                "zillow_actor": raw["zillow_actor"].strip(),
                "zillow_incremental_interval_minutes": interval,
                "source_daily_request_budgets": source_budgets,
                "zillow_results_limit": results_limit,
                "zillow_max_charge_usd": max_charge,
                "incremental_monthly_budget_usd": monthly_budget,
                "incremental_active_start": raw["incremental_active_start"].strip(),
                "incremental_active_end": raw["incremental_active_end"].strip(),
                "incremental_canary_telegram_ids": canary_ids,
            }
        )
        return {
            "incremental_alerts_enabled": candidate.incremental_alerts_enabled,
            "zillow_enabled": candidate.zillow_enabled,
            "zillow_terms_confirmed": raw["zillow_terms_confirmed"],
            "zillow_actor": candidate.zillow_actor,
            "zillow_incremental_interval_minutes": (
                candidate.zillow_incremental_interval_minutes
            ),
            "zillow_daily_request_budget": candidate.source_request_budget("zillow"),
            "zillow_results_limit": candidate.zillow_results_limit,
            "zillow_max_charge_usd": candidate.zillow_max_charge_usd,
            "incremental_monthly_budget_usd": (
                candidate.incremental_monthly_budget_usd
            ),
            "incremental_active_start": candidate.incremental_active_start,
            "incremental_active_end": candidate.incremental_active_end,
            "incremental_canary_telegram_ids": (
                candidate.incremental_canary_telegram_ids
            ),
        }

    @on(Input.Submitted)
    def _enter_submits(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        try:
            result = self._validated()
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                message = "; ".join(
                    str(item.get("msg", "invalid setting")).replace("Value error, ", "")
                    for item in exc.errors()
                )
            else:
                message = str(exc)
            self.query_one("#incremental-form-error", Label).update(message)
            return
        self.dismiss(result)

    def action_cancel_dialog(self) -> None:
        if self._collect() != self._opened_with and not self._discard_armed:
            self._discard_armed = True
            self.query_one("#incremental-form-error", Label).update(
                "Unsaved changes — press Esc again to discard them, or Enter to save."
            )
            return
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)
