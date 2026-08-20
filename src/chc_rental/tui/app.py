"""Textual owner dashboard for the file-based build.

Local-only operator control plane over `Store`: who is on the allowlist, what
each person's searches are, and what the last daily run did. No business
validation lives here — coercion is in `chc_rental.tui.forms` and every rule is
enforced by the Pydantic models.

Layout rules worth keeping (each one was a real clipping bug):
  * `DataTable` gets `height: 1fr` so growing tables scroll instead of pushing
    the action buttons off the bottom of the screen.
  * Every dialog has an explicit natural height capped at 90% of the screen,
    and everything between the title and the button row lives in a
    `.dialog-fields` VerticalScroll with `height: 1fr`. On a short terminal
    the squeeze is absorbed by that scrollable middle; a plain fixed stack
    instead clips from the bottom, which is exactly where Save/Cancel sit.
    (`height: auto` cannot express this: an auto dialog with a `1fr` child
    greedily expands to max-height even when its content is two inputs.)
  * Modal screens set `align: center middle` themselves — Textual's ModalScreen
    stopped centering children, so without it dialogs pin to the top-left.
  * Row containers inside an auto-height dialog need `height: auto`; the default
    `1fr` collapses to one row and clips 3-row Buttons and Switches.
  * Dialog labels need `width: 100%` or they clip mid-word instead of wrapping.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable, Optional, Union
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Switch,
    TabbedContent,
    TabPane,
)

from chc_rental.delivery_history import RoutineDeliveryDay, RoutineDeliveryEntry
from chc_rental.models import DEFAULT_LISTING_CAP, Profile, Settings
from chc_rental.store import Store, StoreError
from chc_rental.test_push import TestPushBlocked, TestPushPlan
from chc_rental.tui.controller import DuplicateError, NotFoundError, TuiController
from chc_rental.tui.alerts import AlertsScreen, ConfirmActionScreen
from chc_rental.tui.forms import FormParsingError, parse_search_form, search_to_form
from chc_rental.tui.status import StatusScreen

DEFAULT_ROOT = os.environ.get("CHC_RENTAL_ROOT", ".")

FORM_ERRORS = (
    FormParsingError,
    ValidationError,
    DuplicateError,
    NotFoundError,
    StoreError,
    TestPushBlocked,
)

# A validator takes the raw form dict and returns an error line to show in the
# modal, or None when the input is good enough to save.
FormValidator = Callable[[dict], Optional[str]]


def _friendly_error(exc: Exception) -> str:
    """Collapse a validation failure into one readable line for a modal label."""
    if isinstance(exc, ValidationError):
        seen: list[str] = []
        for err in exc.errors():
            msg = str(err.get("msg", "")).replace("Value error, ", "").strip()
            if msg and msg not in seen:
                seen.append(msg)
        return "; ".join(seen) or "invalid input"
    return str(exc)


def _sqft_text(low: Optional[int], high: Optional[int]) -> str:
    if low is None and high is None:
        return "any"
    if high is None:
        return f"{low}+"
    if low is None:
        return f"<={high}"
    return f"{low}-{high}"


def _delivery_text(profile: Profile) -> str:
    if profile.delivery_mode.value == "immediate":
        text = f"immediate · {profile.timezone}"
        if profile.quiet_hours_start:
            text += f" · quiet {profile.quiet_hours_start}-{profile.quiet_hours_end}"
        return text
    return f"daily {profile.delivery_time} {profile.timezone}"


class AddPersonScreen(ModalScreen[Optional[dict]]):
    """Reusable add/edit modal for a Telegram ID and display name.

    Enter submits from either field. Escape on a dirty form warns once before
    discarding — silent input loss is the bug this dashboard keeps regrowing.
    """

    BINDINGS = [("escape", "cancel_dialog", "Cancel")]

    def __init__(
        self,
        *,
        title: str = "Add allowlisted person",
        submit_label: str = "Add",
        initial: Optional[dict] = None,
        validator: Optional[FormValidator] = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._submit_label = submit_label
        self._initial = initial or {}
        self._validator = validator

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title)
            yield Label("", id="add-person-error")
            with VerticalScroll(classes="dialog-fields"):
                yield Input(
                    value=str(self._initial.get("telegram_id", "")),
                    placeholder="Telegram user ID (number)",
                    id="telegram_id",
                )
                yield Input(
                    value=str(self._initial.get("display_name", "")),
                    placeholder="Display name",
                    id="display_name",
                )
            with Horizontal():
                yield Button(self._submit_label, id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._opened_with = self._collect()
        self._discard_armed = False
        self.query_one("#telegram_id", Input).focus()

    def _collect(self) -> dict:
        return {
            "telegram_id": self.query_one("#telegram_id", Input).value,
            "display_name": self.query_one("#display_name", Input).value,
        }

    @on(Input.Submitted)
    def _enter_submits(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def action_cancel_dialog(self) -> None:
        if self._collect() != self._opened_with and not self._discard_armed:
            self._discard_armed = True
            self.query_one("#add-person-error", Label).update(
                "Unsaved changes — press Esc again to discard them, or Enter to save."
            )
            return
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        telegram_id = self.query_one("#telegram_id", Input).value.strip()
        display_name = self.query_one("#display_name", Input).value.strip()
        # int() rather than isdigit(): characters like "²" pass isdigit() but
        # explode in int(), and a handler exception takes down the whole app.
        try:
            parsed_id = int(telegram_id)
        except ValueError:
            parsed_id = 0
        if parsed_id <= 0 or not display_name:
            self.query_one("#add-person-error", Label).update(
                "Telegram ID must be a positive whole number and display name is required."
            )
            return
        result = {"telegram_id": parsed_id, "display_name": display_name}
        if self._validator is not None:
            error = self._validator(result)
            if error:
                self.query_one("#add-person-error", Label).update(error)
                return
        self.dismiss(result)


class MemberDetailsScreen(Screen[None]):
    """Edit one allowlisted member and inspect their routine delivery diary."""

    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("ctrl+s", "save_profile", "Save profile"),
    ]

    def __init__(
        self,
        controller: TuiController,
        telegram_id: int,
        *,
        on_change: Optional[Callable[[], None]] = None,
        on_message: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._telegram_id = telegram_id
        self._on_change = on_change
        self._message_callback = on_message
        self._days: dict[str, RoutineDeliveryDay] = {}
        self._entries: dict[str, RoutineDeliveryEntry] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Member details", id="member-details-title")
        yield Label("", id="member-details-error")
        with TabbedContent(initial="member-profile-pane", id="member-details-tabs"):
            with TabPane("Profile", id="member-profile-pane"):
                with VerticalScroll(id="member-profile-fields"):
                    yield Label("Telegram ID")
                    yield Input(id="member-telegram-id")
                    yield Label("Display name")
                    yield Input(id="member-display-name")
                with Horizontal(classes="action-row"):
                    yield Button("Save profile", id="save-member", variant="primary")
            with TabPane("Routine diary", id="member-diary-pane"):
                yield Label(
                    "Confirmed routine pushes, selected historical Test Push records, "
                    "and uncertain attempts — newest first",
                    id="routine-diary-summary",
                    markup=False,
                )
                yield DataTable(id="routine-diary-days")
                yield DataTable(id="routine-diary-entries")
                with VerticalScroll(id="routine-diary-detail-scroll"):
                    yield Label(
                        "Select a routine delivery entry to inspect its exact content.",
                        id="routine-diary-detail",
                        markup=False,
                    )
        with Horizontal(id="member-details-actions", classes="action-row"):
            yield Button("Back", id="member-details-back")
        yield Footer()

    def on_mount(self) -> None:
        days = self.query_one("#routine-diary-days", DataTable)
        days.cursor_type = "row"
        days.add_columns("Date", "Listings", "Notices", "Uncertain")
        entries = self.query_one("#routine-diary-entries", DataTable)
        entries.cursor_type = "row"
        entries.add_columns("Time", "Status", "Filter", "Item")
        self._load_profile()
        self.refresh_diary()

    def _load_profile(self) -> None:
        person = self._controller.get_person(self._telegram_id)
        self.query_one("#member-telegram-id", Input).value = str(person.telegram_id)
        self.query_one("#member-display-name", Input).value = person.display_name
        self.query_one("#member-details-title", Label).update(
            f"Member details — {person.display_name} ({person.telegram_id})"
        )

    def _set_error(self, message: str) -> None:
        self.query_one("#member-details-error", Label).update(message)
        if self._message_callback is not None:
            self._message_callback(message)

    def action_save_profile(self) -> None:
        self._save_profile()

    @on(Input.Submitted, "#member-telegram-id")
    @on(Input.Submitted, "#member-display-name")
    @on(Button.Pressed, "#save-member")
    def _save_profile(self) -> None:
        raw_id = self.query_one("#member-telegram-id", Input).value.strip()
        name = self.query_one("#member-display-name", Input).value.strip()
        try:
            new_id = int(raw_id)
        except ValueError:
            self._set_error("Telegram ID must be a positive whole number.")
            return
        old_id = self._telegram_id
        try:
            outcome = self._controller.update_person(old_id, new_id, name)
        except FORM_ERRORS as exc:
            self._set_error(_friendly_error(exc))
            return
        self._telegram_id = new_id
        self._load_profile()
        self.refresh_diary()
        if self._on_change is not None:
            self._on_change()
        if outcome.id_changed:
            message = (
                f"Updated Telegram ID {old_id} → {new_id}; the new ID starts with "
                "an empty routine diary and fresh suppression history."
            )
            if outcome.outbox_rows_cancelled:
                message += (
                    f" Cancelled {outcome.outbox_rows_cancelled} unsent alert(s)."
                )
        else:
            message = f"Saved member name {name!r}."
        self._set_error(message)

    def refresh_diary(self) -> None:
        table = self.query_one("#routine-diary-days", DataTable)
        table.clear()
        self._days.clear()
        self._entries.clear()
        try:
            days = self._controller.routine_delivery_diary(self._telegram_id)
        except FORM_ERRORS as exc:
            self._set_error(f"Could not read routine diary: {_friendly_error(exc)}")
            self._show_day(None)
            return
        for day in days:
            key = day.local_date.isoformat()
            self._days[key] = day
            table.add_row(
                key,
                str(day.listing_count),
                str(day.notice_count),
                str(day.uncertain_count),
                key=key,
            )
        if days:
            table.move_cursor(row=0)
            self._show_day(days[0])
        else:
            self._show_day(None)
            self.query_one("#routine-diary-summary", Label).update(
                "No routine delivery records are available for this Telegram ID."
            )

    @on(DataTable.RowHighlighted, "#routine-diary-days")
    def _day_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_day(self._days.get(str(event.row_key.value)))

    def _show_day(self, day: RoutineDeliveryDay | None) -> None:
        table = self.query_one("#routine-diary-entries", DataTable)
        table.clear()
        self._entries.clear()
        if day is None:
            self.query_one("#routine-diary-detail", Label).update(
                "No routine delivery entry is selected."
            )
            return
        self.query_one("#routine-diary-summary", Label).update(
            f"{day.local_date.isoformat()} — {day.listing_count} listing(s), "
            f"{day.notice_count} no-results notice(s), "
            f"{day.uncertain_count} uncertain attempt(s)"
        )
        for entry in day.entries:
            key = entry.attempt_id
            self._entries[key] = entry
            try:
                local_time = entry.updated_at.astimezone(
                    ZoneInfo(entry.timezone)
                ).strftime("%H:%M:%S")
            except Exception:
                local_time = entry.updated_at.strftime("%H:%M:%S")
            item = entry.primary_item
            filter_name = item.search_name if item else "Routine notice"
            item_name = item.display_name if item else "No matching rentals"
            status = "ACCEPTED" if entry.status == "accepted" else "UNCERTAIN"
            if entry.channel == "test":
                status = "TEST PUSH · " + status
            elif entry.channel == "incremental":
                status = "INCREMENTAL · " + status
            if entry.legacy:
                status += " · LEGACY"
            table.add_row(
                local_time,
                status,
                filter_name or "—",
                item_name,
                key=key,
            )
        if day.entries:
            table.move_cursor(row=0)
            self._show_entry(day.entries[0])

    @on(DataTable.RowHighlighted, "#routine-diary-entries")
    def _entry_highlighted(self, event: DataTable.RowHighlighted) -> None:
        entry = self._entries.get(str(event.row_key.value))
        if entry is not None:
            self._show_entry(entry)

    def _show_entry(self, entry: RoutineDeliveryEntry) -> None:
        status = "Telegram accepted" if entry.status == "accepted" else (
            "UNCERTAIN — Telegram delivery could not be confirmed; this message "
            "may or may not have arrived and was not retried."
        )
        lines = [
            status,
            f"Routine date: {entry.local_date.isoformat()} ({entry.timezone})",
            f"Delivery type: {entry.channel}",
            f"Attempt: {entry.attempt_id}",
        ]
        if entry.telegram_message_id:
            lines.append(f"Telegram receipt: {entry.telegram_message_id}")
        if entry.legacy:
            lines.append(
                "Legacy record — fields not present in the original ledger remain unknown."
            )
        if entry.error:
            lines.append(f"Delivery note: {entry.error}")
        if entry.message_text:
            lines.extend(("", "Exact outbound content:", entry.message_text))
        elif entry.kind == "listing":
            lines.extend(("", "Exact original message was not retained."))
        for item in entry.items:
            details = [
                item.display_name,
                f"Location: {item.city or 'unknown'}"
                + (f" / {item.district}" if item.district else ""),
                f"Price: ${item.price:,}" if item.price is not None else "Price: unknown",
                (
                    f"Beds/baths: {item.beds} / {item.baths:g}"
                    if item.beds is not None and item.baths is not None
                    else "Beds/baths: unknown"
                ),
                f"Filter: {item.search_name or 'unknown'}",
                f"Source: {item.source or 'unknown'}",
                f"URL: {item.url or 'unavailable'}",
            ]
            lines.extend(("", *details))
        self.query_one("#routine-diary-detail", Label).update("\n".join(lines))

    @on(Button.Pressed, "#member-details-back")
    def _back(self) -> None:
        self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()


class SearchFormScreen(ModalScreen[Optional[dict]]):
    """Create/edit modal covering every field of one search."""

    BINDINGS = [("escape", "cancel_dialog", "Cancel")]

    def __init__(
        self,
        *,
        title: str,
        initial: Optional[dict] = None,
        validator: Optional[FormValidator] = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._initial = initial or {}
        self._validator = validator

    _TEXT_FIELDS = (
        "name", "city", "state", "district", "price_min", "price_max", "property_types",
        "bed_min", "bed_max", "bath_min", "bath_max", "sqft_min", "sqft_max",
        "required_features", "excluded_features", "daily_cap",
    )

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title)
            yield Label("", id="search-form-error")
            with VerticalScroll(id="search-form-fields", classes="dialog-fields"):
                yield from self._compose_fields()
            with Horizontal():
                yield Button("Save", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._opened_with = self._collect()
        self._discard_armed = False
        self.query_one("#name", Input).focus()

    def _collect(self) -> dict:
        data = {field: self.query_one(f"#{field}", Input).value for field in self._TEXT_FIELDS}
        data["active"] = self.query_one("#active", Switch).value
        return data

    @on(Input.Submitted)
    def _enter_submits(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def action_cancel_dialog(self) -> None:
        if self._collect() != self._opened_with and not self._discard_armed:
            self._discard_armed = True
            self.query_one("#search-form-error", Label).update(
                "Unsaved changes — press Esc again to discard them, or Enter to save."
            )
            return
        self.dismiss(None)

    def _compose_fields(self) -> ComposeResult:
        yield Label("Search name")
        yield Input(value=self._initial.get("name", ""), id="name")
        yield Label("City")
        yield Input(value=self._initial.get("city", ""), id="city")
        yield Label("State (name or 2-letter code, e.g. New York or NY — required for source fetches)")
        yield Input(value=self._initial.get("state", ""), id="state")
        yield Label("District (optional; RentCast provides none — leave blank)")
        yield Input(value=self._initial.get("district", ""), id="district")
        yield Label("Price min")
        yield Input(value=self._initial.get("price_min", ""), id="price_min")
        yield Label("Price max")
        yield Input(value=self._initial.get("price_max", ""), id="price_max")
        yield Label(
            "Property types (comma-separated: apartment, house, condo, townhouse, "
            "studio, room, single_family, multi_family)"
        )
        yield Input(value=self._initial.get("property_types", ""), id="property_types")
        yield Label("Bed min")
        yield Input(value=self._initial.get("bed_min", ""), id="bed_min")
        yield Label("Bed max")
        yield Input(value=self._initial.get("bed_max", ""), id="bed_max")
        yield Label("Bath min")
        yield Input(value=self._initial.get("bath_min", ""), id="bath_min")
        yield Label("Bath max")
        yield Input(value=self._initial.get("bath_max", ""), id="bath_max")
        yield Label("Sq ft min (optional, blank = no minimum)")
        yield Input(value=self._initial.get("sqft_min", ""), id="sqft_min")
        yield Label("Sq ft max (optional, blank = no maximum)")
        yield Input(value=self._initial.get("sqft_max", ""), id="sqft_max")
        yield Label("Required features (comma-separated)")
        yield Input(value=self._initial.get("required_features", ""), id="required_features")
        yield Label("Excluded features (comma-separated)")
        yield Input(value=self._initial.get("excluded_features", ""), id="excluded_features")
        yield Label("Listing limit (max matching links per scrape; default 25)")
        yield Input(
            value=self._initial.get("daily_cap", str(DEFAULT_LISTING_CAP)),
            id="daily_cap",
        )
        with Horizontal():
            yield Label("Active")
            yield Switch(value=self._initial.get("active", True), id="active")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        data = self._collect()
        # Validate while the modal is still open so a bad field shows its error
        # here and everything the user typed is preserved, instead of the modal
        # closing and dropping the input with the error hidden on the screen behind.
        if self._validator is not None:
            error = self._validator(data)
            if error:
                self.query_one("#search-form-error", Label).update(error)
                return
        self.dismiss(data)


class DeliveryFormScreen(ModalScreen[Optional[dict]]):
    """Per-person delivery settings.

    The daily push reads only the top section (time, timezone, no-match notice).
    Delivery mode and quiet hours below feed only the incremental-alert path,
    which is currently inactive — they are grouped and labeled as such so the
    dialog never implies they change the daily push. Source on/off and budgets
    live on the main dashboard's Config screen.
    """

    BINDINGS = [("escape", "cancel_dialog", "Cancel")]

    def __init__(self, *, initial: dict, validator: Optional[FormValidator] = None) -> None:
        super().__init__()
        self._initial = initial
        self._validator = validator

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Delivery settings")
            yield Label("", id="delivery-form-error")
            with VerticalScroll(classes="dialog-fields"):
                yield Label("— Daily push —")
                yield Label("Daily notification time (HH:MM)")
                yield Input(value=self._initial.get("delivery_time", "09:00"), id="delivery_time")
                yield Label("Timezone (IANA, e.g. Asia/Taipei)")
                yield Input(value=self._initial.get("timezone", "America/New_York"), id="timezone")
                with Horizontal():
                    yield Label("Notify when nothing matched")
                    yield Switch(
                        value=self._initial.get("notify_on_no_results", False),
                        id="notify_on_no_results",
                    )
                yield Label("— Incremental alerts (inactive; not used by the daily push) —")
                yield Label(
                    f"Incremental outbox: shadow {self._initial.get('shadow_alerts', 0)} · "
                    f"pending {self._initial.get('pending_alerts', 0)} · "
                    f"failed {self._initial.get('failed_alerts', 0)}"
                )
                yield Label("Alert delivery mode")
                yield Select(
                    [("Daily at the selected time", "daily"), ("Immediate", "immediate")],
                    value=self._initial.get("delivery_mode", "daily"),
                    allow_blank=False,
                    id="delivery_mode",
                )
                yield Label("Immediate-mode quiet-hours start (HH:MM; both blank = off)")
                yield Input(
                    value=self._initial.get("quiet_hours_start", ""),
                    id="quiet_hours_start",
                )
                yield Label("Immediate-mode quiet-hours end (HH:MM; both blank = off)")
                yield Input(
                    value=self._initial.get("quiet_hours_end", ""),
                    id="quiet_hours_end",
                )
            with Horizontal():
                yield Button("Save", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._opened_with = self._collect()
        self._discard_armed = False
        self.query_one("#delivery_time", Input).focus()

    def _collect(self) -> dict:
        return {
            "delivery_mode": str(self.query_one("#delivery_mode", Select).value),
            "delivery_time": self.query_one("#delivery_time", Input).value,
            "timezone": self.query_one("#timezone", Input).value,
            "quiet_hours_start": self.query_one("#quiet_hours_start", Input).value,
            "quiet_hours_end": self.query_one("#quiet_hours_end", Input).value,
            "notify_on_no_results": self.query_one("#notify_on_no_results", Switch).value,
        }

    @on(Input.Submitted)
    def _enter_submits(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def action_cancel_dialog(self) -> None:
        if self._collect() != self._opened_with and not self._discard_armed:
            self._discard_armed = True
            self.query_one("#delivery-form-error", Label).update(
                "Unsaved changes — press Esc again to discard them, or Enter to save."
            )
            return
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        raw = self._collect()
        data = {
            "delivery_mode": raw["delivery_mode"],
            "delivery_time": raw["delivery_time"].strip(),
            "timezone": raw["timezone"].strip(),
            "quiet_hours_start": raw["quiet_hours_start"].strip() or None,
            "quiet_hours_end": raw["quiet_hours_end"].strip() or None,
            "notify_on_no_results": raw["notify_on_no_results"],
        }
        if self._validator is not None:
            error = self._validator(data)
            if error:
                self.query_one("#delivery-form-error", Label).update(error)
                return
        self.dismiss(data)


class SearchesScreen(Screen[None]):
    """Every saved search belonging to one allowlisted person."""

    # Six buttons cannot fit at 80 columns with Textual's default 16-cell
    # minimum, so the labels are short and every action also has a key.
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("n", "new_search", "New"),
        ("e", "edit_search", "Edit"),
        ("t", "toggle_search", "Toggle"),
        ("d", "delete_search", "Delete"),
        ("y", "delivery_settings", "Delivery"),
    ]

    def __init__(
        self,
        controller: TuiController,
        telegram_id: int,
        display_name: str,
        *,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._telegram_id = telegram_id
        self._display_name = display_name
        self._on_change = on_change

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(f"Searches for {self._display_name} ({self._telegram_id})", id="searches-title")
        yield Label("", id="searches-error")
        yield DataTable(id="searches-table")
        with Horizontal(classes="action-row"):
            yield Button("New", id="new-search", variant="primary")
            yield Button("Edit", id="edit-search")
            yield Button("Toggle", id="toggle-search")
            yield Button("Delete", id="delete-search", variant="error")
            yield Button("Delivery", id="delivery-settings")
            yield Button("Back", id="back")
        yield Footer()

    def action_new_search(self) -> None:
        self._new_search()

    def action_edit_search(self) -> None:
        self._edit_search()

    def action_toggle_search(self) -> None:
        self._toggle_search()

    def action_delete_search(self) -> None:
        self._delete_search()

    def action_delivery_settings(self) -> None:
        self._delivery_settings()

    def on_mount(self) -> None:
        table = self.query_one("#searches-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "#", "Name", "Location", "Price", "Types", "Beds", "Baths",
            "Sqft", "Features", "Cap", "Active",
        )
        self.refresh_searches()

    def refresh_searches(self) -> None:
        table = self.query_one("#searches-table", DataTable)
        table.clear()
        try:
            searches = self._controller.list_searches(self._telegram_id)
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return
        for index, search in enumerate(searches):
            location = f"{search.city}, {search.state or '—'}"
            if search.district:
                location += f" / {search.district}"
            features = ", ".join(
                [
                    *(f"+{item}" for item in search.required_features),
                    *(f"-{item}" for item in search.excluded_features),
                ]
            ) or "any"
            table.add_row(
                str(index + 1),
                search.name,
                location,
                f"{search.price_min}-{search.price_max}",
                ", ".join(item.value for item in search.property_types),
                f"{search.bed_min}-{search.bed_max}",
                f"{search.bath_min:g}-{search.bath_max:g}",
                _sqft_text(search.sqft_min, search.sqft_max),
                features,
                str(search.daily_cap),
                "yes" if search.active else "no",
                key=str(index),
            )
        person = self._controller.get_person(self._telegram_id)
        profile = person.profile
        self.query_one("#searches-title", Label).update(
            f"Searches for {person.display_name} ({person.telegram_id}) — "
            f"{_delivery_text(profile)}"
            + ("  [no-match notices on]" if profile.notify_on_no_results else "")
        )

    def _selected_index(self) -> Optional[int]:
        table = self.query_one("#searches-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return int(row_key.value)

    def _set_error(self, message: str) -> None:
        self.query_one("#searches-error", Label).update(message)

    def _changed(self) -> None:
        """Refresh this screen and the suspended parent allowlist table."""
        self.refresh_searches()
        if self._on_change is not None:
            self._on_change()

    def _validate_search_form(self, data: dict, *, exclude_index: Optional[int] = None) -> Optional[str]:
        """Full save-time check, run inside the modal so errors keep it open."""
        try:
            search = parse_search_form(data)
            existing = self._controller.list_searches(self._telegram_id)
        except FORM_ERRORS as exc:
            return _friendly_error(exc)
        for position, other in enumerate(existing):
            if position == exclude_index:
                continue
            if other.name.lower() == search.name.lower():
                return f"This profile already has a search named {search.name!r}."
        return None

    def _validate_delivery_form(self, data: dict) -> Optional[str]:
        try:
            Profile(
                delivery_mode=data["delivery_mode"],
                delivery_time=data["delivery_time"],
                timezone=data["timezone"],
                quiet_hours_start=data["quiet_hours_start"],
                quiet_hours_end=data["quiet_hours_end"],
                notify_on_no_results=data["notify_on_no_results"],
            )
        except ValidationError as exc:
            return _friendly_error(exc)
        return None

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#new-search")
    def _new_search(self) -> None:
        def handle(result: Optional[dict]) -> None:
            if result is None:
                return
            try:
                self._controller.add_search(self._telegram_id, result)
            except FORM_ERRORS as exc:
                self._set_error(str(exc))
                return
            self._set_error(f"Saved search {result['name'].strip()!r}.")
            self._changed()

        self.app.push_screen(
            SearchFormScreen(title="New search", validator=self._validate_search_form), handle
        )

    @on(Button.Pressed, "#edit-search")
    def _edit_search(self) -> None:
        index = self._selected_index()
        if index is None:
            self._set_error("Select a search first.")
            return
        try:
            current = self._controller.list_searches(self._telegram_id)[index]
        except (FORM_ERRORS, IndexError) as exc:
            self._set_error(str(exc))
            return

        def handle(result: Optional[dict]) -> None:
            if result is None:
                return
            try:
                self._controller.update_search(self._telegram_id, index, result)
            except FORM_ERRORS as exc:
                self._set_error(str(exc))
                return
            self._set_error(f"Saved search {result['name'].strip()!r}.")
            self._changed()

        self.app.push_screen(
            SearchFormScreen(
                title=f"Edit search: {current.name}",
                initial=search_to_form(current),
                validator=lambda data: self._validate_search_form(data, exclude_index=index),
            ),
            handle,
        )

    @on(Button.Pressed, "#toggle-search")
    def _toggle_search(self) -> None:
        index = self._selected_index()
        if index is None:
            self._set_error("Select a search first.")
            return
        try:
            self._controller.toggle_search(self._telegram_id, index)
            search = self._controller.list_searches(self._telegram_id)[index]
        except (FORM_ERRORS, IndexError) as exc:
            self._set_error(str(exc))
            return
        self._set_error(f"{search.name!r} {'enabled' if search.active else 'disabled'}.")
        self._changed()

    @on(Button.Pressed, "#delete-search")
    def _delete_search(self) -> None:
        index = self._selected_index()
        if index is None:
            self._set_error("Select a search first.")
            return
        try:
            name = self._controller.list_searches(self._telegram_id)[index].name
            self._controller.delete_search(self._telegram_id, index)
        except (FORM_ERRORS, IndexError) as exc:
            self._set_error(str(exc))
            return
        self._set_error(f"Deleted search {name!r}.")
        self._changed()

    @on(Button.Pressed, "#delivery-settings")
    def _delivery_settings(self) -> None:
        try:
            profile = self._controller.get_person(self._telegram_id).profile
            outbox = self._controller.recipient_outbox_counts(self._telegram_id)
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return

        def handle(result: Optional[dict]) -> None:
            if result is None:
                return
            try:
                self._controller.update_delivery(
                    self._telegram_id,
                    delivery_time=result["delivery_time"],
                    timezone_name=result["timezone"],
                    delivery_mode=result["delivery_mode"],
                    quiet_hours_start=result["quiet_hours_start"],
                    quiet_hours_end=result["quiet_hours_end"],
                    notify_on_no_results=result["notify_on_no_results"],
                )
            except FORM_ERRORS as exc:
                self._set_error(str(exc))
                return
            message = "Delivery settings saved."
            if self._controller.person_pushes_before_scrape(self._telegram_id):
                s = self._controller.settings()
                message += (
                    f"  ⚠ push time is before the scrape time {s.scrape_time} "
                    f"{s.scrape_timezone}; the first push may be empty."
                )
            self._set_error(message)
            self._changed()

        self.app.push_screen(
            DeliveryFormScreen(
                initial={
                    "delivery_mode": profile.delivery_mode.value,
                    "delivery_time": profile.delivery_time,
                    "timezone": profile.timezone,
                    "quiet_hours_start": profile.quiet_hours_start or "",
                    "quiet_hours_end": profile.quiet_hours_end or "",
                    "notify_on_no_results": profile.notify_on_no_results,
                    "shadow_alerts": outbox.get("shadow", 0),
                    "pending_alerts": (
                        outbox.get("pending", 0) + outbox.get("retry_wait", 0)
                    ),
                    "failed_alerts": (
                        outbox.get("failed", 0) + outbox.get("uncertain", 0)
                    ),
                },
                validator=self._validate_delivery_form,
            ),
            handle,
        )


class PushTimeScreen(ModalScreen[Optional[dict]]):
    """Edit ONE person's daily push time + timezone, straight from the main page.

    Deliberately minimal. The full delivery screen grew mode/quiet-hours/alert
    routing and became the thing owners found confusing, so the single setting
    they change most — when the daily push goes out — gets its own small dialog
    reachable directly from the people list. Every other delivery setting is
    preserved untouched by the caller.
    """

    BINDINGS = [("escape", "cancel_dialog", "Cancel")]

    def __init__(
        self,
        *,
        display_name: str,
        initial: dict,
        validator: Optional[FormValidator] = None,
    ) -> None:
        super().__init__()
        self._display_name = display_name
        self._initial = initial
        self._validator = validator

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Daily push time — {self._display_name}")
            yield Label("", id="push-time-error")
            with VerticalScroll(classes="dialog-fields"):
                yield Label("Push time (HH:MM, 24-hour)")
                yield Input(value=self._initial.get("delivery_time", "09:00"), id="delivery_time")
                yield Label("Timezone (IANA, e.g. America/New_York)")
                yield Input(value=self._initial.get("timezone", "America/New_York"), id="timezone")
            with Horizontal():
                yield Button("Save", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._opened_with = self._collect()
        self._discard_armed = False
        self.query_one("#delivery_time", Input).focus()

    def _collect(self) -> dict:
        return {
            "delivery_time": self.query_one("#delivery_time", Input).value,
            "timezone": self.query_one("#timezone", Input).value,
        }

    @on(Input.Submitted)
    def _enter_submits(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def action_cancel_dialog(self) -> None:
        if self._collect() != self._opened_with and not self._discard_armed:
            self._discard_armed = True
            self.query_one("#push-time-error", Label).update(
                "Unsaved changes — press Esc again to discard them, or Enter to save."
            )
            return
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        raw = self._collect()
        data = {"delivery_time": raw["delivery_time"].strip(), "timezone": raw["timezone"].strip()}
        if self._validator is not None:
            error = self._validator(data)
            if error:
                self.query_one("#push-time-error", Label).update(error)
                return
        self.dismiss(data)


class ConfigScreen(ModalScreen[Optional[dict]]):
    """Every daily-run global setting in one place, so nothing needs a YAML edit.

    Mirrors IncrementalSettingsScreen's safe contract: parse + candidate-validate
    the whole Settings model while the modal stays open, so a bad field (or an
    incoherent on-disk incremental config) shows a readable error instead of
    writing or crashing.
    """

    BINDINGS = [("escape", "cancel_dialog", "Cancel")]

    def __init__(self, *, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    def compose(self) -> ComposeResult:
        s = self._settings
        with Vertical(id="dialog"):
            yield Label("Daily run configuration")
            yield Label("", id="config-error")
            with VerticalScroll(classes="dialog-fields"):
                yield Label("— Delivery —")
                with Horizontal():
                    yield Label("Send real Telegram pushes (off = dry run)")
                    yield Switch(value=s.live_push_enabled, id="live_push_enabled")
                yield Label("— Fetch schedule —")
                yield Label("Scrape time (HH:MM, 24h) — when the daily fetch runs")
                yield Input(value=s.scrape_time, id="scrape_time")
                yield Label("Scrape timezone (IANA, e.g. America/New_York)")
                yield Input(value=s.scrape_timezone, id="scrape_timezone")
                yield Label("— Budgets —")
                yield Label("Global daily request budget")
                yield Input(
                    value=str(s.global_daily_request_budget), id="global_daily_request_budget"
                )
                yield Label("Per-source daily request budget (fallback)")
                yield Input(
                    value=str(s.per_source_daily_request_budget),
                    id="per_source_daily_request_budget",
                )
                yield Label("— Source: Zillow (Apify) —")
                with Horizontal():
                    yield Label("Zillow source enabled")
                    yield Switch(value=s.zillow_enabled, id="zillow_enabled")
                yield Label(
                    "Zillow is a third-party managed scraper. Confirm you accept its "
                    "terms/cost responsibility before first enabling it."
                )
                with Horizontal():
                    yield Label("I confirm the Zillow scraper warning")
                    yield Switch(value=s.zillow_enabled, id="zillow_terms_confirmed")
                yield Label("Apify actor ID")
                yield Input(value=s.zillow_actor, id="zillow_actor")
                yield Label("Zillow requests per day")
                yield Input(
                    value=str(s.source_request_budget("zillow")), id="zillow_daily_request_budget"
                )
                yield Label("Max listings pulled per run")
                yield Input(value=str(s.zillow_results_limit), id="zillow_results_limit")
                yield Label("Max Apify charge per run (USD)")
                yield Input(value=str(s.zillow_max_charge_usd), id="zillow_max_charge_usd")
                yield Label("Zillow request timeout (seconds)")
                yield Input(value=str(s.zillow_timeout_seconds), id="zillow_timeout_seconds")
                yield Label("— Operator alerts —")
                yield Label("Operator alert Telegram ID (blank = Telegram alerts off)")
                yield Input(
                    value=(
                        ""
                        if s.operator_alert_telegram_id is None
                        else str(s.operator_alert_telegram_id)
                    ),
                    id="operator_alert_telegram_id",
                )
            with Horizontal():
                yield Button("Save", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._opened_with = self._collect()
        self._discard_armed = False
        self.query_one("#scrape_time", Input).focus()

    def _collect(self) -> dict:
        q = self.query_one
        return {
            "live_push_enabled": q("#live_push_enabled", Switch).value,
            "scrape_time": q("#scrape_time", Input).value,
            "scrape_timezone": q("#scrape_timezone", Input).value,
            "global_daily_request_budget": q("#global_daily_request_budget", Input).value,
            "per_source_daily_request_budget": q("#per_source_daily_request_budget", Input).value,
            "zillow_enabled": q("#zillow_enabled", Switch).value,
            "zillow_terms_confirmed": q("#zillow_terms_confirmed", Switch).value,
            "zillow_actor": q("#zillow_actor", Input).value,
            "zillow_daily_request_budget": q("#zillow_daily_request_budget", Input).value,
            "zillow_results_limit": q("#zillow_results_limit", Input).value,
            "zillow_max_charge_usd": q("#zillow_max_charge_usd", Input).value,
            "zillow_timeout_seconds": q("#zillow_timeout_seconds", Input).value,
            "operator_alert_telegram_id": q(
                "#operator_alert_telegram_id", Input
            ).value,
        }

    def _validated(self) -> dict:
        raw = self._collect()
        try:
            global_budget = int(raw["global_daily_request_budget"].strip())
            per_source_budget = int(raw["per_source_daily_request_budget"].strip())
            zillow_budget = int(raw["zillow_daily_request_budget"].strip())
            results_limit = int(raw["zillow_results_limit"].strip())
            timeout = int(raw["zillow_timeout_seconds"].strip())
            max_charge = float(raw["zillow_max_charge_usd"].strip())
            operator_text = raw["operator_alert_telegram_id"].strip()
            operator_id = int(operator_text) if operator_text else None
        except ValueError as exc:
            raise ValueError("budgets, limits, timeout, charge, and IDs must be numbers") from exc
        if (
            raw["zillow_enabled"]
            and not self._settings.zillow_enabled
            and not raw["zillow_terms_confirmed"]
        ):
            raise ValueError("confirm the Zillow scraper warning before enabling it")
        source_budgets = dict(self._settings.source_daily_request_budgets)
        source_budgets["zillow"] = zillow_budget
        candidate = Settings.model_validate(
            {
                **self._settings.model_dump(mode="python"),
                "live_push_enabled": raw["live_push_enabled"],
                "scrape_time": raw["scrape_time"].strip(),
                "scrape_timezone": raw["scrape_timezone"].strip(),
                "global_daily_request_budget": global_budget,
                "per_source_daily_request_budget": per_source_budget,
                "operator_alert_telegram_id": operator_id,
                "zillow_enabled": raw["zillow_enabled"],
                "zillow_actor": raw["zillow_actor"].strip(),
                "source_daily_request_budgets": source_budgets,
                "zillow_results_limit": results_limit,
                "zillow_max_charge_usd": max_charge,
                "zillow_timeout_seconds": timeout,
            }
        )
        return {
            "live_push_enabled": candidate.live_push_enabled,
            "scrape_time": candidate.scrape_time,
            "scrape_timezone": candidate.scrape_timezone,
            "global_daily_request_budget": candidate.global_daily_request_budget,
            "per_source_daily_request_budget": candidate.per_source_daily_request_budget,
            "operator_alert_telegram_id": candidate.operator_alert_telegram_id,
            "zillow_enabled": candidate.zillow_enabled,
            "zillow_terms_confirmed": raw["zillow_terms_confirmed"],
            "zillow_actor": candidate.zillow_actor,
            "zillow_daily_request_budget": candidate.source_request_budget("zillow"),
            "zillow_results_limit": candidate.zillow_results_limit,
            "zillow_max_charge_usd": candidate.zillow_max_charge_usd,
            "zillow_timeout_seconds": candidate.zillow_timeout_seconds,
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
            self.query_one("#config-error", Label).update(_friendly_error(exc))
            return
        self.dismiss(result)

    def action_cancel_dialog(self) -> None:
        if self._collect() != self._opened_with and not self._discard_armed:
            self._discard_armed = True
            self.query_one("#config-error", Label).update(
                "Unsaved changes — press Esc again to discard them, or Enter to save."
            )
            return
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class TestPushPreviewScreen(ModalScreen[bool]):
    """Scrollable, plain-text preview of the exact outbound Telegram parts."""

    __test__ = False
    BINDINGS = [("escape", "cancel_dialog", "Cancel")]

    def __init__(self, plan: TestPushPlan) -> None:
        super().__init__()
        self._plan = plan

    def compose(self) -> ComposeResult:
        total = len(self._plan.messages)
        with Vertical(id="test-push-preview-dialog"):
            yield Label("Review exact Telegram test push", markup=False)
            yield Label(
                self._plan.confirmation_text,
                id="test-push-preview-summary",
                markup=False,
            )
            with VerticalScroll(id="test-push-preview-scroll"):
                for part, message in enumerate(self._plan.messages, 1):
                    if total > 1:
                        yield Label(
                            f"Telegram part {part}/{total}",
                            classes="test-push-part-heading",
                            markup=False,
                        )
                    yield Label(
                        message,
                        id=f"test-push-preview-part-{part}",
                        classes="test-push-preview-part",
                        markup=False,
                    )
            with Horizontal():
                yield Button(
                    (
                        "Resend seen listings"
                        if self._plan.repeat_override
                        else "Send exactly this"
                    ),
                    id="confirm-action",
                    variant="error" if self._plan.repeat_override else "warning",
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


class OwnerDashboardApp(App[None]):
    """Allowlist management plus per-person search screens."""

    CSS = """
    /* Textual's ModalScreen no longer centers its children by default. */
    AddPersonScreen, SearchFormScreen, DeliveryFormScreen, IncrementalSettingsScreen,
    ConfirmActionScreen, PushTimeScreen, ConfigScreen, TestPushPreviewScreen {
        align: center middle;
    }
    #dialog {
        padding: 1 2;
        width: 64;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        background: $panel;
        border: thick $primary;
    }
    /* Natural height per dialog, in rows: 4 chrome (border + vertical padding)
       + 1 title + 1 error + fields + 3 button row. On a shorter terminal,
       max-height clamps the dialog and the 1fr fields region absorbs the
       whole squeeze by scrolling — never the button row. If a dialog gains a
       field, bump its number here or the fields just scroll a little. */
    AddPersonScreen #dialog { height: 15; }    /* fields: 2 inputs = 6 rows */
    PushTimeScreen #dialog { height: 16; }     /* 2 labels + 2 inputs = 6 rows */
    DeliveryFormScreen #dialog { height: 32; }
    IncrementalSettingsScreen #dialog { height: 90%; }
    #confirm-dialog {
        padding: 1 2;
        width: 64;
        max-width: 95%;
        height: 16;
        max-height: 90%;
        background: $panel;
        border: thick $warning;
    }
    #confirm-message { width: 100%; height: 1fr; }
    #confirm-dialog Horizontal { height: auto; }
    #test-push-preview-dialog {
        padding: 1 2;
        width: 90%;
        max-width: 120;
        height: 100%;
        max-height: 100%;
        background: $panel;
        border: thick $warning;
    }
    #test-push-preview-summary {
        width: 100%;
        height: auto;
        max-height: 7;
    }
    #test-push-preview-scroll {
        width: 100%;
        height: 1fr;
        margin: 1 0;
        padding: 0 1;
        border: round $secondary;
    }
    .test-push-part-heading {
        width: 100%;
        height: auto;
        color: $warning;
        text-style: bold;
    }
    .test-push-preview-part {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }
    #test-push-preview-dialog > Horizontal { height: auto; }
    MemberDetailsScreen {
        layout: vertical;
    }
    #member-details-title,
    #member-details-error,
    #routine-diary-summary,
    #routine-diary-detail {
        width: 100%;
        height: auto;
    }
    #member-details-error {
        max-height: 3;
    }
    #member-details-tabs {
        height: 1fr;
    }
    #member-profile-fields {
        height: 1fr;
        padding: 1 2;
    }
    #member-profile-pane > Horizontal,
    #member-details-actions {
        height: auto;
    }
    #routine-diary-summary {
        max-height: 2;
    }
    #routine-diary-days {
        height: 5;
        min-height: 3;
    }
    #routine-diary-entries {
        height: 6;
        min-height: 3;
    }
    #routine-diary-detail-scroll {
        height: 1fr;
        min-height: 4;
        border: round $secondary;
        padding: 0 1;
    }
    SearchFormScreen #dialog { height: 90%; }  /* fields always overflow; give them everything */
    ConfigScreen #dialog { height: 90%; }       /* many fields; scroll the middle */
    /* The scrollable middle of every dialog. `1fr` is what lets a short
       terminal squeeze this region while the button row below stays visible;
       a fixed stack instead clips the buttons off the bottom of the dialog. */
    .dialog-fields {
        height: 1fr;
    }
    #dialog > Label,
    .dialog-fields > Label {
        width: 100%;
    }
    /* Long validation messages must wrap, not run off the right edge. */
    #dashboard-error,
    #searches-error {
        width: 100%;
    }
    #dialog Horizontal,
    .action-row {
        height: auto;
    }
    /* Textual's default Button min-width of 16 puts a six-button toolbar past
       the right edge at 80 columns, leaving the last one unreachable. */
    .action-row Button {
        min-width: 11;
    }
    /* Nine direct member controls fit at the supported 80-column minimum
       when their labels use only the button border as horizontal chrome.
       Fractional widths distribute every spare terminal column across the
       toolbar instead of leaving a block of unused space on the right. */
    #people-actions {
        width: 100%;
    }
    #people-actions Button {
        min-width: 0;
    }
    /* Content-weighted fractions keep every label on one line at 80 columns,
       then expand proportionally all the way to the right edge on wider
       terminals. */
    #add-person { width: 7fr; }
    #edit-person { width: 9fr; }
    #remove-person { width: 11fr; }
    #toggle-person { width: 10fr; }
    #set-push-time { width: 15fr; }
    #open-searches { width: 12fr; }
    #test-push { width: 15fr; }
    #open-config { width: 10fr; }
    #open-status { width: 11fr; }
    /* Test push is intentionally yellow, distinct from the orange Pause
       warning and red destructive Remove action. */
    #test-push {
        color: #000000;
        background: #FFD54F;
        border-top: tall #FFF3A0;
        border-bottom: tall #B38B00;
    }
    #test-push:hover, #test-push:focus {
        color: #000000;
        background: #FFCA28;
        border-top: tall #FFE082;
        border-bottom: tall #9C7800;
    }
    DataTable {
        height: 1fr;
    }
    #source-status-table {
        height: 5;
        min-height: 5;
    }
    #query-health-table, #incremental-outbox-table {
        height: 1fr;
        min-height: 5;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("p", "set_push_time", "Push time"),
        ("c", "open_config", "Config"),
        ("e", "edit_person", "Edit"),
        ("r", "remove_person", "Remove"),
        ("x", "test_push", "Test push"),
    ]

    def __init__(self, root: Union[str, Path] = DEFAULT_ROOT) -> None:
        super().__init__()
        self._root = Path(root)
        self.store: Optional[Store] = None
        self.controller: Optional[TuiController] = None
        self._test_push_preflight_active = False
        self._test_push_confirmation_open = False
        self._test_push_active = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("CHC Rental — Dashboard", id="dashboard-title")
        yield Label("", id="dashboard-error")
        yield DataTable(id="people-table")
        with Horizontal(id="people-actions", classes="action-row"):
            yield Button("Add", id="add-person", variant="primary")
            yield Button("Edit", id="edit-person")
            yield Button("Remove", id="remove-person", variant="error")
            yield Button("Pause", id="toggle-person", variant="warning")
            yield Button("Push time", id="set-push-time")
            yield Button("Filters", id="open-searches")
            yield Button("Test push", id="test-push", variant="warning")
            yield Button("Config", id="open-config")
            yield Button("Status", id="open-status")
        yield Footer()

    def on_mount(self) -> None:
        self.store = Store(self._root)
        self.store.initialize()
        self.controller = TuiController(self.store)

        # Textual's CSS parser rejects zero for line-pad even though the runtime
        # style property supports it. Removing that default padding is what lets
        # nine full labels fit without shrinking the buttons below 3 rows.
        for button in self.query("#people-actions Button"):
            button.styles.line_pad = 0

        table = self.query_one("#people-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Telegram ID", "Display name", "Searches", "Push time", "Allowlisted")
        self.refresh_people()

    def refresh_people(self) -> None:
        table = self.query_one("#people-table", DataTable)
        table.clear()
        try:
            people = self.controller.list_people()
        except StoreError as exc:
            self._set_error(str(exc))
            return
        for person in people:
            profile = person.profile
            table.add_row(
                str(person.telegram_id),
                person.display_name,
                f"{len(profile.active_searches())}/{len(profile.searches)}",
                _delivery_text(profile),
                "yes" if person.active else "no",
                key=str(person.telegram_id),
            )

    def _selected_person_id(self) -> Optional[int]:
        table = self.query_one("#people-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return int(row_key.value)

    def _set_error(self, message: str) -> None:
        self.query_one("#dashboard-error", Label).update(message)

    def _member_change_blocked(self) -> bool:
        if not (
            self._test_push_preflight_active
            or self._test_push_active
            or self._test_push_confirmation_open
        ):
            return False
        if self._test_push_preflight_active:
            state = "checking Telegram credentials"
        elif self._test_push_active:
            state = "in progress"
        else:
            state = "awaiting confirmation"
        self._set_error(
            f"Member changes are blocked while a test push is {state}."
        )
        return True

    def action_test_push(self) -> None:
        self._test_push()

    @on(Button.Pressed, "#test-push")
    def _test_push(self) -> None:
        if (
            self._test_push_preflight_active
            or self._test_push_active
            or self._test_push_confirmation_open
        ):
            if self._test_push_preflight_active:
                state = "checking Telegram credentials"
            elif self._test_push_active:
                state = "in progress"
            else:
                state = "awaiting confirmation"
            self._set_error(f"A test push is already {state}.")
            return
        telegram_id = self._selected_person_id()
        if telegram_id is None:
            self._set_error("Select a person first.")
            return

        self._test_push_preflight_active = True
        self._set_error(
            f"Checking Telegram bot access to selected ID {telegram_id}…"
        )
        self._test_push_preflight_worker(telegram_id)

    @work(thread=True, group="test-push-preflight", exit_on_error=False)
    def _test_push_preflight_worker(self, telegram_id: int) -> None:
        try:
            plan = self.controller.prepare_test_push(telegram_id)
        except Exception as exc:
            error = f"Test push blocked: {_friendly_error(exc)}"
            self.call_from_thread(self._finish_test_push_preflight, None, error)
            return
        self.call_from_thread(self._finish_test_push_preflight, plan, None)

    def _finish_test_push_preflight(
        self,
        plan: Optional[TestPushPlan],
        error: Optional[str],
    ) -> None:
        self._test_push_preflight_active = False
        if error is not None or plan is None:
            self._set_error(error or "Test push preflight failed unexpectedly.")
            return

        def handle(confirmed: bool) -> None:
            self._test_push_confirmation_open = False
            if not confirmed:
                self._set_error("Test push cancelled; nothing was sent.")
                return
            if self._test_push_active:
                self._set_error("A test push is already in progress.")
                return
            self._test_push_active = True
            self._set_error(
                f"Sending test push to {plan.display_name} ({plan.telegram_id})…"
            )
            self._test_push_worker(plan)

        self._test_push_confirmation_open = True
        self.push_screen(TestPushPreviewScreen(plan), handle)

    @work(thread=True, group="test-push-send", exit_on_error=False)
    def _test_push_worker(self, plan: TestPushPlan) -> None:
        try:
            message = self.controller.send_test_push(plan).dashboard_message
        except Exception as exc:
            message = f"Test push failed unexpectedly: {type(exc).__name__}: {exc}"
        self.call_from_thread(self._finish_test_push, message)

    def _finish_test_push(self, message: str) -> None:
        self._test_push_active = False
        self._set_error(message)

    @on(Button.Pressed, "#add-person")
    def _add_person(self) -> None:
        def handle(result: Optional[dict]) -> None:
            if result is None:
                return
            try:
                self.controller.add_person(result["telegram_id"], result["display_name"])
            except FORM_ERRORS as exc:
                self._set_error(str(exc))
                return
            self._set_error(f"Added {result['display_name']!r} to the allowlist.")
            self.refresh_people()

        def validate(result: dict) -> Optional[str]:
            try:
                self.controller.get_person(result["telegram_id"])
            except NotFoundError:
                return None
            except FORM_ERRORS as exc:
                return str(exc)
            return f"telegram id {result['telegram_id']} is already on the allowlist"

        self.push_screen(AddPersonScreen(validator=validate), handle)

    def action_edit_person(self) -> None:
        self._edit_person()

    @on(Button.Pressed, "#edit-person")
    def _edit_person(self) -> None:
        if self._member_change_blocked():
            return
        old_telegram_id = self._selected_person_id()
        if old_telegram_id is None:
            self._set_error("Select a person first.")
            return
        try:
            self.controller.get_person(old_telegram_id)
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return
        self.push_screen(
            MemberDetailsScreen(
                self.controller,
                old_telegram_id,
                on_change=self.refresh_people,
                on_message=self._set_error,
            )
        )

    @on(Button.Pressed, "#toggle-person")
    def _toggle_person(self) -> None:
        telegram_id = self._selected_person_id()
        if telegram_id is None:
            self._set_error("Select a person first.")
            return
        try:
            person = self.controller.get_person(telegram_id)
            self.controller.set_person_active(telegram_id, not person.active)
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return
        state = "allowlisted" if not person.active else "removed from the allowlist"
        self._set_error(f"{person.display_name!r} {state}.")
        self.refresh_people()

    def _validate_push_time(self, data: dict) -> Optional[str]:
        try:
            Profile(delivery_time=data["delivery_time"], timezone=data["timezone"])
        except ValidationError as exc:
            return _friendly_error(exc)
        return None

    def action_set_push_time(self) -> None:
        self._set_push_time()

    @on(Button.Pressed, "#set-push-time")
    def _set_push_time(self) -> None:
        telegram_id = self._selected_person_id()
        if telegram_id is None:
            self._set_error("Select a person first.")
            return
        try:
            person = self.controller.get_person(telegram_id)
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return
        profile = person.profile

        def handle(result: Optional[dict]) -> None:
            if result is None:
                return
            try:
                # Only time + timezone change here; every other delivery setting
                # (mode, quiet hours, no-results notice) is passed through as-is.
                self.controller.update_delivery(
                    telegram_id,
                    delivery_time=result["delivery_time"],
                    timezone_name=result["timezone"],
                    delivery_mode=profile.delivery_mode.value,
                    quiet_hours_start=profile.quiet_hours_start,
                    quiet_hours_end=profile.quiet_hours_end,
                    notify_on_no_results=profile.notify_on_no_results,
                )
            except FORM_ERRORS as exc:
                self._set_error(str(exc))
                return
            message = (
                f"Push time for {person.display_name!r} set to "
                f"{result['delivery_time']} {result['timezone']}."
            )
            if self.controller.person_pushes_before_scrape(telegram_id):
                s = self.controller.settings()
                message += (
                    f"  ⚠ this is before the scrape time {s.scrape_time} "
                    f"{s.scrape_timezone}; the first push may be empty."
                )
            self._set_error(message)
            self.refresh_people()

        self.push_screen(
            PushTimeScreen(
                display_name=person.display_name,
                initial={"delivery_time": profile.delivery_time, "timezone": profile.timezone},
                validator=self._validate_push_time,
            ),
            handle,
        )

    @on(Button.Pressed, "#open-searches")
    def _open_searches(self) -> None:
        telegram_id = self._selected_person_id()
        if telegram_id is None:
            self._set_error("Select a person first.")
            return
        try:
            person = self.controller.get_person(telegram_id)
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return
        self._set_error("")
        self.push_screen(
            SearchesScreen(
                self.controller,
                person.telegram_id,
                person.display_name,
                on_change=self.refresh_people,
            )
        )

    @on(Button.Pressed, "#open-status")
    def _open_status(self) -> None:
        self._set_error("")
        self.push_screen(StatusScreen(self.controller))

    @on(Button.Pressed, "#open-alerts")
    def _open_alerts(self) -> None:
        self._set_error("")
        self.push_screen(AlertsScreen(self.controller))

    def action_open_config(self) -> None:
        self._open_config()

    @on(Button.Pressed, "#open-config")
    def _open_config(self) -> None:
        try:
            settings = self.controller.settings()
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return

        def handle(result: Optional[dict]) -> None:
            if result is None:
                return
            try:
                self.controller.update_run_settings(**result)
            except FORM_ERRORS as exc:
                self._set_error(_friendly_error(exc))
                return
            message = "Config saved."
            early = self.controller.people_pushing_before_scrape()
            if early:
                names = ", ".join(name for _, name in early)
                message += (
                    f"  ⚠ {names} push before the scrape time "
                    "and may get an empty morning."
                )
            self._set_error(message)
            self.refresh_people()

        self.push_screen(ConfigScreen(settings=settings), handle)

    def action_remove_person(self) -> None:
        if self._member_change_blocked():
            return
        telegram_id = self._selected_person_id()
        if telegram_id is None:
            self._set_error("Select a person first.")
            return
        try:
            person = self.controller.get_person(telegram_id)
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return

        def handle(confirmed: Optional[bool]) -> None:
            if not confirmed:
                return
            try:
                outcome = self.controller.remove_person(telegram_id)
            except FORM_ERRORS as exc:
                self._set_error(str(exc))
                return
            message = (
                f"Removed {person.display_name!r}, their searches, and recipient state"
            )
            if outcome.outbox_rows_cancelled:
                message += f"; cancelled {outcome.outbox_rows_cancelled} unsent alert(s)"
            self._set_error(message + ". Completed delivery audits were retained.")
            self.refresh_people()

        self.push_screen(
            ConfirmActionScreen(
                title=f"Remove {person.display_name}?",
                message=(
                    "Permanently deletes this member, all searches, seen history, "
                    "and incremental-canary membership. Unsent alerts are cancelled; "
                    "completed delivery and test-push audits are retained."
                ),
                confirm_label="Remove",
            ),
            handle,
        )

    @on(Button.Pressed, "#remove-person")
    def _remove_person_button(self) -> None:
        self.action_remove_person()


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="dashboard.sh",
        description="CHC Rental owner dashboard — allowlist and per-person searches.",
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        help="Data directory holding config/ and state/ (default: %(default)s).",
    )
    args = parser.parse_args()
    OwnerDashboardApp(root=args.root).run()


if __name__ == "__main__":
    run()
