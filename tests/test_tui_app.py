"""Terminal-free smoke tests for the Phase 2 Textual owner dashboard."""

import asyncio
import importlib

import pytest

from chc_rental.tui.app import (
    AddUserScreen,
    OwnerDashboardApp,
    ProfileFormScreen,
    ProfilesScreen,
    run,
)


def test_app_module_imports_and_exposes_run():
    module = importlib.import_module("chc_rental.tui.app")
    assert callable(module.run)
    assert module.run is run


def test_console_script_entry_point_resolves_without_starting_app():
    # Mirrors what the `chc-rental-tui` console script does at import time,
    # without invoking App.run() (which would need a real terminal).
    module = importlib.import_module("chc_rental.tui.app")
    assert hasattr(module, "run")


def test_cli_help_argument_parsing_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["chc-rental-tui", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "chc-rental-tui" in captured.out


def test_dashboard_boots_headless_with_empty_allowlist():
    async def scenario():
        app = OwnerDashboardApp(db_path=":memory:")
        async with app.run_test(size=(120, 80)) as pilot:
            assert app.controller is not None
            assert app.controller.list_users() == []

    asyncio.run(scenario())


def test_add_user_then_open_profiles_then_create_profile_end_to_end():
    async def scenario():
        app = OwnerDashboardApp(db_path=":memory:")
        async with app.run_test(size=(120, 80)) as pilot:
            # Add a user through the modal, matching the real UI flow.
            await pilot.click("#add-user")
            assert isinstance(app.screen, AddUserScreen)
            app.screen.query_one("#telegram_user_id").value = "111"
            app.screen.query_one("#display_name").value = "Peter"
            await pilot.click("#submit")
            await pilot.pause()

            users = app.controller.list_users()
            assert len(users) == 1
            assert users[0].telegram_user_id == 111
            assert users[0].display_name == "Peter"

            # Select the user row and open their profiles screen.
            await pilot.click("#open-profiles")
            await pilot.pause()
            assert isinstance(app.screen, ProfilesScreen)

            # Create a profile covering every model preference field.
            await pilot.click("#new-profile")
            await pilot.pause()
            assert isinstance(app.screen, ProfileFormScreen)

            form = app.screen
            form.query_one("#profile_name").value = "Downtown"
            form.query_one("#city").value = "Austin"
            form.query_one("#district").value = "North Loop"
            form.query_one("#price_min").value = "1000"
            form.query_one("#price_max").value = "2000"
            form.query_one("#property_types").value = "apartment, condo"
            form.query_one("#bed_min").value = "1"
            form.query_one("#bed_max").value = "2"
            form.query_one("#bath_min").value = "1.0"
            form.query_one("#bath_max").value = "2.0"
            form.query_one("#required_features").value = "parking"
            form.query_one("#excluded_features").value = "ground floor"
            form.query_one("#daily_cap").value = "5"
            form.query_one("#delivery_time").value = "18:30"
            form.query_one("#timezone").value = "Europe/London"
            await pilot.click("#submit")
            await pilot.pause()

            profiles = app.controller.list_profiles(111)
            assert len(profiles) == 1
            profile = profiles[0]
            assert profile.profile_name == "Downtown"
            assert profile.city == "Austin"
            assert profile.district == "North Loop"
            assert profile.price_min == 1000
            assert profile.price_max == 2000
            assert profile.property_types == ["apartment", "condo"]
            assert profile.bed_min == 1
            assert profile.bed_max == 2
            assert profile.bath_min == 1.0
            assert profile.bath_max == 2.0
            assert profile.required_features == ["parking"]
            assert profile.excluded_features == ["ground floor"]
            assert profile.daily_cap == 5
            assert profile.active is True
            assert profile.delivery_time == "18:30"
            assert profile.timezone == "Europe/London"

    asyncio.run(scenario())


def test_new_profile_form_defaults_delivery_fields_when_left_blank():
    async def scenario():
        app = OwnerDashboardApp(db_path=":memory:")
        async with app.run_test(size=(120, 80)) as pilot:
            await pilot.click("#add-user")
            app.screen.query_one("#telegram_user_id").value = "111"
            app.screen.query_one("#display_name").value = "Peter"
            await pilot.click("#submit")
            await pilot.pause()

            await pilot.click("#open-profiles")
            await pilot.pause()
            await pilot.click("#new-profile")
            await pilot.pause()

            form = app.screen
            form.query_one("#profile_name").value = "Downtown"
            form.query_one("#city").value = "Austin"
            form.query_one("#price_min").value = "1000"
            form.query_one("#price_max").value = "2000"
            form.query_one("#property_types").value = "apartment"
            form.query_one("#bed_min").value = "1"
            form.query_one("#bed_max").value = "2"
            form.query_one("#bath_min").value = "1.0"
            form.query_one("#bath_max").value = "2.0"
            form.query_one("#daily_cap").value = "5"
            await pilot.click("#submit")
            await pilot.pause()

            profile = app.controller.list_profiles(111)[0]
            assert profile.delivery_time == "09:00"
            assert profile.timezone == "America/New_York"

    asyncio.run(scenario())


def test_edit_profile_form_is_prefilled_and_can_update_delivery_fields():
    async def scenario():
        app = OwnerDashboardApp(db_path=":memory:")
        async with app.run_test(size=(120, 80)) as pilot:
            await pilot.click("#add-user")
            app.screen.query_one("#telegram_user_id").value = "111"
            app.screen.query_one("#display_name").value = "Peter"
            await pilot.click("#submit")
            await pilot.pause()

            await pilot.click("#open-profiles")
            await pilot.pause()
            await pilot.click("#new-profile")
            await pilot.pause()

            form = app.screen
            form.query_one("#profile_name").value = "Downtown"
            form.query_one("#city").value = "Austin"
            form.query_one("#price_min").value = "1000"
            form.query_one("#price_max").value = "2000"
            form.query_one("#property_types").value = "apartment"
            form.query_one("#bed_min").value = "1"
            form.query_one("#bed_max").value = "2"
            form.query_one("#bath_min").value = "1.0"
            form.query_one("#bath_max").value = "2.0"
            form.query_one("#daily_cap").value = "5"
            await pilot.click("#submit")
            await pilot.pause()

            await pilot.click("#edit-profile")
            await pilot.pause()
            edit_form = app.screen
            assert edit_form.query_one("#delivery_time").value == "09:00"
            assert edit_form.query_one("#timezone").value == "America/New_York"

            edit_form.query_one("#delivery_time").value = "20:15"
            edit_form.query_one("#timezone").value = "Asia/Tokyo"
            await pilot.click("#submit")
            await pilot.pause()

            profile = app.controller.list_profiles(111)[0]
            assert profile.delivery_time == "20:15"
            assert profile.timezone == "Asia/Tokyo"

    asyncio.run(scenario())
