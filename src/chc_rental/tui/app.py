"""Phase 2 minimal Textual owner dashboard.

Local-only operator control plane for the allowlist and preference profiles.
No business validation lives here: form coercion and rule enforcement stay in
`chc_rental.tui.controller` / `chc_rental.tui.forms` / `chc_rental.repositories`.
This module only wires widgets to `TuiController` calls.
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path
from typing import Optional, Union

from pydantic import ValidationError
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Switch

from chc_rental.db import Database
from chc_rental.errors import (
    DuplicateProfileNameError,
    NotAllowlistedError,
    ProfileAccessDeniedError,
    ProfileNotFoundError,
)
from chc_rental.tui.controller import TuiController
from chc_rental.tui.forms import FormParsingError
from chc_rental.tui.status import StatusScreen

DEFAULT_DB_PATH = os.environ.get("CHC_RENTAL_DB_PATH", "data/chc_rental.sqlite3")

PROFILE_FIELDS = (
    "profile_name",
    "city",
    "district",
    "price_min",
    "price_max",
    "property_types",
    "bed_min",
    "bed_max",
    "bath_min",
    "bath_max",
    "sqft_min",
    "sqft_max",
    "required_features",
    "excluded_features",
    "daily_cap",
    "delivery_time",
    "timezone",
    "notify_on_no_results",
)

PROFILE_FORM_ERRORS = (
    FormParsingError,
    ValidationError,
    DuplicateProfileNameError,
    NotAllowlistedError,
    ProfileNotFoundError,
    ProfileAccessDeniedError,
)


def _sqft_range_text(sqft_min: Optional[int], sqft_max: Optional[int]) -> str:
    if sqft_min is None and sqft_max is None:
        return "any"
    if sqft_max is None:
        return f"{sqft_min}+"
    if sqft_min is None:
        return f"<={sqft_max}"
    return f"{sqft_min}-{sqft_max}"


def _profile_to_form(profile) -> dict:
    return {
        "profile_name": profile.profile_name,
        "city": profile.city,
        "district": profile.district or "",
        "price_min": str(profile.price_min),
        "price_max": str(profile.price_max),
        "property_types": ", ".join(pt.value for pt in profile.property_types),
        "bed_min": str(profile.bed_min),
        "bed_max": str(profile.bed_max),
        "bath_min": str(profile.bath_min),
        "bath_max": str(profile.bath_max),
        "sqft_min": "" if profile.sqft_min is None else str(profile.sqft_min),
        "sqft_max": "" if profile.sqft_max is None else str(profile.sqft_max),
        "required_features": ", ".join(profile.required_features),
        "excluded_features": ", ".join(profile.excluded_features),
        "daily_cap": str(profile.daily_cap),
        "delivery_time": profile.delivery_time,
        "timezone": profile.timezone,
        "notify_on_no_results": profile.notify_on_no_results,
        "active": profile.active,
    }


class AddUserScreen(ModalScreen[Optional[dict]]):
    """Modal to add a numeric allowlisted Telegram user ID + display name."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add allowlisted user")
            yield Label("", id="add-user-error")
            yield Input(placeholder="Telegram user ID (number)", id="telegram_user_id")
            yield Input(placeholder="Display name", id="display_name")
            with Horizontal():
                yield Button("Add", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        telegram_user_id = self.query_one("#telegram_user_id", Input).value.strip()
        display_name = self.query_one("#display_name", Input).value.strip()
        if not telegram_user_id.isdigit() or int(telegram_user_id) <= 0 or not display_name:
            self.query_one("#add-user-error", Label).update(
                "Telegram user ID must be a positive whole number and display name is required."
            )
            return
        self.dismiss({"telegram_user_id": int(telegram_user_id), "display_name": display_name})


class ProfileFormScreen(ModalScreen[Optional[dict]]):
    """Create/edit modal covering every preference-profile field."""

    def __init__(self, *, title: str, initial: Optional[dict] = None) -> None:
        super().__init__()
        self._title = title
        self._initial = initial or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="form-dialog"):
            yield Label(self._title)
            yield Label("", id="profile-form-error")
            with VerticalScroll(id="profile-form-fields"):
                yield from self._compose_fields()
            with Horizontal():
                yield Button("Save", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    def _compose_fields(self) -> ComposeResult:
        yield Label("Profile name")
        yield Input(value=self._initial.get("profile_name", ""), id="profile_name")
        yield Label("City")
        yield Input(value=self._initial.get("city", ""), id="city")
        yield Label("District (optional)")
        yield Input(value=self._initial.get("district", ""), id="district")
        yield Label("Price min")
        yield Input(value=self._initial.get("price_min", ""), id="price_min")
        yield Label("Price max")
        yield Input(value=self._initial.get("price_max", ""), id="price_max")
        yield Label(
            "Property types (comma-separated: apartment, house, condo, townhouse, studio, room)"
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
        yield Label("Daily cap")
        yield Input(value=self._initial.get("daily_cap", ""), id="daily_cap")
        yield Label("Daily notification time (HH:MM)")
        yield Input(value=self._initial.get("delivery_time", "09:00"), id="delivery_time")
        yield Label("Timezone (IANA, e.g. America/New_York)")
        yield Input(value=self._initial.get("timezone", "America/New_York"), id="timezone")
        with Horizontal():
            yield Label("Notify when no listings match")
            yield Switch(value=self._initial.get("notify_on_no_results", False), id="notify_on_no_results")
        with Horizontal():
            yield Label("Active")
            yield Switch(value=self._initial.get("active", True), id="active")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        data = {
            field: self.query_one(f"#{field}", Input).value
            for field in PROFILE_FIELDS
            if field != "notify_on_no_results"
        }
        data["notify_on_no_results"] = self.query_one("#notify_on_no_results", Switch).value
        data["active"] = self.query_one("#active", Switch).value
        self.dismiss(data)


class ProfilesScreen(Screen[None]):
    """List/create/edit/delete profiles for one allowlisted Telegram user."""

    BINDINGS = [("escape", "go_back", "Back")]

    def __init__(self, controller: TuiController, telegram_user_id: int, display_name: str) -> None:
        super().__init__()
        self._controller = controller
        self._telegram_user_id = telegram_user_id
        self._display_name = display_name

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(f"Profiles for {self._display_name} ({self._telegram_user_id})")
        yield Label("", id="profiles-error")
        yield DataTable(id="profiles-table")
        with Horizontal(classes="action-row"):
            yield Button("New profile", id="new-profile", variant="primary")
            yield Button("Edit profile", id="edit-profile")
            yield Button("Delete profile", id="delete-profile", variant="error")
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#profiles-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "Name", "City", "Price", "Beds", "Baths", "Sqft", "Cap", "Active")
        self.refresh_profiles()

    def refresh_profiles(self) -> None:
        table = self.query_one("#profiles-table", DataTable)
        table.clear()
        for profile in self._controller.list_profiles(self._telegram_user_id):
            table.add_row(
                str(profile.id),
                profile.profile_name,
                profile.city,
                f"{profile.price_min}-{profile.price_max}",
                f"{profile.bed_min}-{profile.bed_max}",
                f"{profile.bath_min}-{profile.bath_max}",
                _sqft_range_text(profile.sqft_min, profile.sqft_max),
                str(profile.daily_cap),
                "yes" if profile.active else "no",
                key=str(profile.id),
            )

    def _selected_profile_id(self) -> Optional[int]:
        table = self.query_one("#profiles-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return int(row_key.value)

    def _set_error(self, message: str) -> None:
        self.query_one("#profiles-error", Label).update(message)

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#new-profile")
    def _new_profile(self) -> None:
        def handle(result: Optional[dict]) -> None:
            if result is None:
                return
            try:
                self._controller.create_profile(self._telegram_user_id, result)
            except PROFILE_FORM_ERRORS as exc:
                self._set_error(str(exc))
                return
            self._set_error("")
            self.refresh_profiles()

        self.app.push_screen(ProfileFormScreen(title="New profile"), handle)

    @on(Button.Pressed, "#edit-profile")
    def _edit_profile(self) -> None:
        profile_id = self._selected_profile_id()
        if profile_id is None:
            self._set_error("Select a profile first.")
            return
        try:
            current = self._controller.get_profile(self._telegram_user_id, profile_id)
        except PROFILE_FORM_ERRORS as exc:
            self._set_error(str(exc))
            return

        def handle(result: Optional[dict]) -> None:
            if result is None:
                return
            try:
                self._controller.update_profile(self._telegram_user_id, profile_id, result)
            except PROFILE_FORM_ERRORS as exc:
                self._set_error(str(exc))
                return
            self._set_error("")
            self.refresh_profiles()

        self.app.push_screen(
            ProfileFormScreen(
                title=f"Edit profile: {current.profile_name}",
                initial=_profile_to_form(current),
            ),
            handle,
        )

    @on(Button.Pressed, "#delete-profile")
    def _delete_profile(self) -> None:
        profile_id = self._selected_profile_id()
        if profile_id is None:
            self._set_error("Select a profile first.")
            return
        try:
            self._controller.delete_profile(self._telegram_user_id, profile_id)
        except PROFILE_FORM_ERRORS as exc:
            self._set_error(str(exc))
            return
        self._set_error("")
        self.refresh_profiles()


class OwnerDashboardApp(App[None]):
    """Local owner dashboard: allowlist management + per-user profile screens."""

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
    /* The profile form keeps its fields in a scroll region with the
       Save/Cancel row pinned below it, so the actions are always visible. */
    .form-dialog {
        height: 90%;
    }
    #profile-form-fields {
        height: 1fr;
    }
    /* Labels wrap instead of clipping at the dialog edge. */
    #dialog > Label,
    #profile-form-fields > Label {
        width: 100%;
    }
    /* Horizontal defaults to 1fr; inside an auto-height dialog that collapses
       to 1 row and clips the 3-row Switch/Buttons, so size rows to content. */
    #dialog Horizontal,
    .action-row {
        height: auto;
    }
    /* Tables scroll internally instead of growing and pushing the action
       buttons off the bottom of the screen. */
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, db_path: Union[str, Path] = DEFAULT_DB_PATH) -> None:
        super().__init__()
        self._db_path = db_path
        self._db: Optional[Database] = None
        self.controller: Optional[TuiController] = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("CHC Rental — Owner Dashboard")
        yield Label("", id="dashboard-error")
        yield DataTable(id="users-table")
        with Horizontal(classes="action-row"):
            yield Button("Add user", id="add-user", variant="primary")
            yield Button("Deactivate user", id="deactivate-user", variant="error")
            yield Button("Open profiles", id="open-profiles")
            yield Button("Status", id="open-status")
        yield Footer()

    def on_mount(self) -> None:
        if str(self._db_path) != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = Database(self._db_path)
        self.controller = TuiController(self._db)

        table = self.query_one("#users-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Telegram ID", "Display name", "Active")
        self.refresh_users()

    def on_unmount(self) -> None:
        if self._db is not None:
            self._db.close()

    def refresh_users(self) -> None:
        table = self.query_one("#users-table", DataTable)
        table.clear()
        for user in self.controller.list_users():
            table.add_row(
                str(user.telegram_user_id),
                user.display_name,
                "yes" if user.active else "no",
                key=str(user.telegram_user_id),
            )

    def _selected_user_id(self) -> Optional[int]:
        table = self.query_one("#users-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return int(row_key.value)

    def _set_error(self, message: str) -> None:
        self.query_one("#dashboard-error", Label).update(message)

    @on(Button.Pressed, "#add-user")
    def _add_user(self) -> None:
        def handle(result: Optional[dict]) -> None:
            if result is None:
                return
            try:
                self.controller.add_user(result["telegram_user_id"], result["display_name"])
            except ValueError as exc:
                self._set_error(str(exc))
                return
            self._set_error("")
            self.refresh_users()

        self.push_screen(AddUserScreen(), handle)

    @on(Button.Pressed, "#deactivate-user")
    def _deactivate_user(self) -> None:
        user_id = self._selected_user_id()
        if user_id is None:
            self._set_error("Select a user first.")
            return
        try:
            self.controller.deactivate_user(user_id)
        except ValueError as exc:
            self._set_error(str(exc))
            return
        self._set_error("")
        self.refresh_users()

    @on(Button.Pressed, "#open-profiles")
    def _open_profiles(self) -> None:
        user_id = self._selected_user_id()
        if user_id is None:
            self._set_error("Select a user first.")
            return
        user = next(
            (u for u in self.controller.list_users() if u.telegram_user_id == user_id), None
        )
        if user is None:
            self._set_error("User not found.")
            return
        self._set_error("")
        self.push_screen(ProfilesScreen(self.controller, user.telegram_user_id, user.display_name))

    @on(Button.Pressed, "#open-status")
    def _open_status(self) -> None:
        self.push_screen(StatusScreen(self.controller, date.today().isoformat()))


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="chc-rental-tui",
        description="CHC Rental owner dashboard — local Textual TUI for the allowlist and preference profiles.",
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help="Path to the local SQLite database file (default: %(default)s).",
    )
    args = parser.parse_args()
    OwnerDashboardApp(db_path=args.db_path).run()


if __name__ == "__main__":
    run()
