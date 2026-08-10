"""Textual owner dashboard for the file-based build.

Local-only operator control plane over `Store`: who is on the allowlist, what
each person's searches are, and what the last daily run did. No business
validation lives here — coercion is in `chc_rental.tui.forms` and every rule is
enforced by the Pydantic models.

Layout rules worth keeping (each one was a real clipping bug):
  * `DataTable` gets `height: 1fr` so growing tables scroll instead of pushing
    the action buttons off the bottom of the screen.
  * Row containers inside an auto-height dialog need `height: auto`; the default
    `1fr` collapses to one row and clips 3-row Buttons and Switches.
  * Dialog labels need `width: 100%` or they clip mid-word instead of wrapping.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional, Union

from pydantic import ValidationError
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Switch

from chc_rental.store import Store, StoreError
from chc_rental.tui.controller import DuplicateError, NotFoundError, TuiController
from chc_rental.tui.forms import FormParsingError, search_to_form
from chc_rental.tui.status import StatusScreen

DEFAULT_ROOT = os.environ.get("CHC_RENTAL_ROOT", ".")

FORM_ERRORS = (FormParsingError, ValidationError, DuplicateError, NotFoundError, StoreError)


def _sqft_text(low: Optional[int], high: Optional[int]) -> str:
    if low is None and high is None:
        return "any"
    if high is None:
        return f"{low}+"
    if low is None:
        return f"<={high}"
    return f"{low}-{high}"


class AddPersonScreen(ModalScreen[Optional[dict]]):
    """Modal to allowlist a numeric Telegram id plus a display name."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add allowlisted person")
            yield Label("", id="add-person-error")
            yield Input(placeholder="Telegram user ID (number)", id="telegram_id")
            yield Input(placeholder="Display name", id="display_name")
            with Horizontal():
                yield Button("Add", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        telegram_id = self.query_one("#telegram_id", Input).value.strip()
        display_name = self.query_one("#display_name", Input).value.strip()
        if not telegram_id.isdigit() or int(telegram_id) <= 0 or not display_name:
            self.query_one("#add-person-error", Label).update(
                "Telegram ID must be a positive whole number and display name is required."
            )
            return
        self.dismiss({"telegram_id": int(telegram_id), "display_name": display_name})


class SearchFormScreen(ModalScreen[Optional[dict]]):
    """Create/edit modal covering every field of one search."""

    def __init__(self, *, title: str, initial: Optional[dict] = None) -> None:
        super().__init__()
        self._title = title
        self._initial = initial or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="form-dialog"):
            yield Label(self._title)
            yield Label("", id="search-form-error")
            with VerticalScroll(id="search-form-fields"):
                yield from self._compose_fields()
            with Horizontal():
                yield Button("Save", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    def _compose_fields(self) -> ComposeResult:
        yield Label("Search name")
        yield Input(value=self._initial.get("name", ""), id="name")
        yield Label("City")
        yield Input(value=self._initial.get("city", ""), id="city")
        yield Label("District (optional)")
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
        text_fields = (
            "name", "city", "district", "price_min", "price_max", "property_types",
            "bed_min", "bed_max", "bath_min", "bath_max", "sqft_min", "sqft_max",
            "required_features", "excluded_features", "daily_cap",
        )
        data = {field: self.query_one(f"#{field}", Input).value for field in text_fields}
        data["active"] = self.query_one("#active", Switch).value
        self.dismiss(data)


class DeliveryFormScreen(ModalScreen[Optional[dict]]):
    """Edit when and where a person's daily push lands."""

    def __init__(self, *, initial: dict) -> None:
        super().__init__()
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Delivery settings")
            yield Label("", id="delivery-form-error")
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

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        self.dismiss(
            {
                "delivery_time": self.query_one("#delivery_time", Input).value.strip(),
                "timezone": self.query_one("#timezone", Input).value.strip(),
                "notify_on_no_results": self.query_one("#notify_on_no_results", Switch).value,
            }
        )


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

    def __init__(self, controller: TuiController, telegram_id: int, display_name: str) -> None:
        super().__init__()
        self._controller = controller
        self._telegram_id = telegram_id
        self._display_name = display_name

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
        table.add_columns("#", "Name", "City", "Price", "Beds", "Baths", "Sqft", "Cap", "Active")
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
            table.add_row(
                str(index + 1),
                search.name,
                search.city + (f"/{search.district}" if search.district else ""),
                f"{search.price_min}-{search.price_max}",
                f"{search.bed_min}-{search.bed_max}",
                f"{search.bath_min:g}-{search.bath_max:g}",
                _sqft_text(search.sqft_min, search.sqft_max),
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
            self._set_error("")
            self.refresh_searches()

        self.app.push_screen(SearchFormScreen(title="New search"), handle)

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
            self._set_error("")
            self.refresh_searches()

        self.app.push_screen(
            SearchFormScreen(title=f"Edit search: {current.name}", initial=search_to_form(current)),
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
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return
        self._set_error("")
        self.refresh_searches()

    @on(Button.Pressed, "#delete-search")
    def _delete_search(self) -> None:
        index = self._selected_index()
        if index is None:
            self._set_error("Select a search first.")
            return
        try:
            self._controller.delete_search(self._telegram_id, index)
        except FORM_ERRORS as exc:
            self._set_error(str(exc))
            return
        self._set_error("")
        self.refresh_searches()

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
            self._set_error("")
            self.refresh_searches()

        self.app.push_screen(
            DeliveryFormScreen(
                initial={
                    "delivery_time": profile.delivery_time,
                    "timezone": profile.timezone,
                    "notify_on_no_results": profile.notify_on_no_results,
                }
            ),
            handle,
        )


class OwnerDashboardApp(App[None]):
    """Allowlist management plus per-person search screens."""

    CSS = """
    #dialog {
        padding: 1 2;
        width: 64;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        background: $panel;
        border: thick $primary;
    }
    .form-dialog {
        height: 90%;
    }
    #search-form-fields {
        height: 1fr;
    }
    #dialog > Label,
    #search-form-fields > Label {
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
            self._set_error("")
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
        self._set_error("")
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
        self.push_screen(SearchesScreen(self.controller, person.telegram_id, person.display_name))

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
