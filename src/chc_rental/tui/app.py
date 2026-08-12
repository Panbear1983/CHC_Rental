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

from pydantic import ValidationError
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Switch

from chc_rental.models import Profile
from chc_rental.store import Store, StoreError
from chc_rental.tui.controller import DuplicateError, NotFoundError, TuiController
from chc_rental.tui.forms import FormParsingError, parse_search_form, search_to_form
from chc_rental.tui.status import StatusScreen

DEFAULT_ROOT = os.environ.get("CHC_RENTAL_ROOT", ".")

FORM_ERRORS = (FormParsingError, ValidationError, DuplicateError, NotFoundError, StoreError)

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


class AddPersonScreen(ModalScreen[Optional[dict]]):
    """Modal to allowlist a numeric Telegram id plus a display name.

    Enter submits from either field. Escape on a dirty form warns once before
    discarding — silent input loss is the bug this dashboard keeps regrowing.
    """

    BINDINGS = [("escape", "cancel_dialog", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add allowlisted person")
            yield Label("", id="add-person-error")
            with VerticalScroll(classes="dialog-fields"):
                yield Input(placeholder="Telegram user ID (number)", id="telegram_id")
                yield Input(placeholder="Display name", id="display_name")
            with Horizontal():
                yield Button("Add", id="submit", variant="primary")
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
                "Unsaved changes — press Esc again to discard them, or Enter to add."
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
        self.dismiss({"telegram_id": parsed_id, "display_name": display_name})


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
        yield Label("Daily cap (max listings per day from this search)")
        yield Input(value=self._initial.get("daily_cap", ""), id="daily_cap")
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
    """Edit when and where a person's daily push lands."""

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
            "delivery_time": raw["delivery_time"].strip(),
            "timezone": raw["timezone"].strip(),
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
            f"daily at {profile.delivery_time} {profile.timezone}"
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
                delivery_time=data["delivery_time"],
                timezone=data["timezone"],
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
                    notify_on_no_results=result["notify_on_no_results"],
                )
            except FORM_ERRORS as exc:
                self._set_error(str(exc))
                return
            self._set_error("Delivery settings saved.")
            self._changed()

        self.app.push_screen(
            DeliveryFormScreen(
                initial={
                    "delivery_time": profile.delivery_time,
                    "timezone": profile.timezone,
                    "notify_on_no_results": profile.notify_on_no_results,
                },
                validator=self._validate_delivery_form,
            ),
            handle,
        )


class OwnerDashboardApp(App[None]):
    """Allowlist management plus per-person search screens."""

    CSS = """
    /* Textual's ModalScreen no longer centers its children by default. */
    AddPersonScreen, SearchFormScreen, DeliveryFormScreen {
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
    DeliveryFormScreen #dialog { height: 20; } /* fields: 2 labels + 2 inputs + switch row = 11 rows */
    SearchFormScreen #dialog { height: 90%; }  /* fields always overflow; give them everything */
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
    DataTable {
        height: 1fr;
    }
    #source-status-table {
        height: 5;
        min-height: 5;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, root: Union[str, Path] = DEFAULT_ROOT) -> None:
        super().__init__()
        self._root = Path(root)
        self.store: Optional[Store] = None
        self.controller: Optional[TuiController] = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("CHC Rental — Owner Dashboard", id="dashboard-title")
        yield Label("", id="dashboard-error")
        yield DataTable(id="people-table")
        with Horizontal(classes="action-row"):
            yield Button("Add person", id="add-person", variant="primary")
            yield Button("Toggle allowlist", id="toggle-person", variant="error")
            yield Button("Open searches", id="open-searches")
            yield Button("Status", id="open-status")
        yield Footer()

    def on_mount(self) -> None:
        self.store = Store(self._root)
        self.store.initialize()
        self.controller = TuiController(self.store)

        table = self.query_one("#people-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Telegram ID", "Display name", "Searches", "Delivery", "Allowlisted")
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
                f"{profile.delivery_time} {profile.timezone}",
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

        self.push_screen(AddPersonScreen(), handle)

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


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="chc-rental-tui",
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
