"""Headless TUI tests, including the crash and clipping regressions."""

from __future__ import annotations

import asyncio

from textual.errors import NoWidget
from textual.widgets import Button, DataTable, Input, Label, Switch

from chc_rental.models import Allowlist, Profile
from chc_rental.store import Store
from chc_rental.tui.app import (
    AddPersonScreen,
    DeliveryFormScreen,
    OwnerDashboardApp,
    SearchesScreen,
    SearchFormScreen,
)
from chc_rental.tui.controller import TuiController
from chc_rental.tui.status import StatusScreen

from tests.conftest import make_person, make_search

SEARCH_FORM = {
    "name": "Downtown 2BR",
    "city": "Austin",
    "state": "TX",
    "district": "",
    "price_min": "1500",
    "price_max": "3500",
    "property_types": "apartment, condo",
    "bed_min": "2",
    "bed_max": "3",
    "bath_min": "1",
    "bath_max": "2",
    "sqft_min": "",
    "sqft_max": "",
    "required_features": "",
    "excluded_features": "",
    "daily_cap": "5",
    "active": True,
}


def run(coro):
    return asyncio.run(coro)


def test_dashboard_boots_with_an_empty_allowlist(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 30)):
            assert app.controller is not None
            assert app.controller.list_people() == []

    run(scenario())


def test_add_person_through_the_modal(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click("#add-person")
            app.screen.query_one("#telegram_id", Input).value = "12345"
            app.screen.query_one("#display_name", Input).value = "Peter"
            await pilot.click("#submit")
            await pilot.pause()
            people = app.controller.list_people()
            assert len(people) == 1
            assert people[0].telegram_id == 12345

    run(scenario())


def test_add_search_end_to_end(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.add_person(12345, "Peter")
            app.refresh_people()
            await pilot.pause()
            await pilot.click("#open-searches")
            await pilot.pause()
            assert isinstance(app.screen, SearchesScreen)

            await pilot.click("#new-search")
            await pilot.pause()
            form = app.screen
            for field, value in SEARCH_FORM.items():
                if field == "active":
                    continue
                form.query_one(f"#{field}", Input).value = value
            await pilot.click("#submit")
            await pilot.pause()

            searches = app.controller.list_searches(12345)
            assert len(searches) == 1
            assert searches[0].name == "Downtown 2BR"
            assert searches[0].city == "Austin"
            assert searches[0].state == "TX"
            table = app.screen.query_one("#searches-table", DataTable)
            assert table.row_count == 1
            assert str(table.get_row_at(0)[1]) == "Downtown 2BR"

            # The parent allowlist screen remains mounted behind this one. Its
            # search count used to stay at 0/0 until the entire app restarted.
            app.pop_screen()
            await pilot.pause()
            people_table = app.screen.query_one("#people-table", DataTable)
            assert str(people_table.get_row_at(0)[2]) == "1/1"

    run(scenario())


def test_status_screen_survives_a_deactivated_person(tmp_path):
    """The old dashboard died with NotAllowlistedError after any deactivation."""

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.add_person(111, "Stays")
            app.controller.add_person(222, "Removed")
            app.controller.set_person_active(222, False)
            app.refresh_people()
            await pilot.pause()

            await pilot.click("#open-status")
            await pilot.pause()
            assert isinstance(app.screen, StatusScreen)
            summary = str(app.screen.query_one("#people-summary", Label).render())
            assert "1 allowlisted / 2 total" in summary
            sources = app.screen.query_one("#source-status-table", DataTable)
            assert sources.row_count == 2
            assert [str(sources.get_row_at(i)[0]) for i in range(2)] == [
                "rentcast",
                "zillow",
            ]

    run(scenario())


def test_toggle_allowlist_button_round_trips(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            app.controller.add_person(111, "Peter")
            app.refresh_people()
            await pilot.pause()
            await pilot.click("#toggle-person")
            await pilot.pause()
            assert app.controller.get_person(111).active is False
            # Two pilot.click calls on one widget inside the double-click window
            # arrive as a chained click and the second press never fires, so the
            # repeat needs real time between them.
            await asyncio.sleep(0.6)
            await pilot.click("#toggle-person")
            await pilot.pause()
            assert app.controller.get_person(111).active is True

    run(scenario())


def _clipped_buttons(app, tag: str) -> list[str]:
    """Return one problem per button that is not fully visible on screen.

    Region checks alone are not enough: a button inside a too-small dialog
    keeps its full 3-row region and is silently cropped by the container, so
    the compositor's clip rectangle is what must be compared.
    """
    problems = []
    for button in app.screen.query(Button):
        region = button.region
        if region.width == 0 or region.height < 3:
            problems.append(f"{tag} #{button.id} collapsed to {region}")
            continue
        if region.bottom > app.size.height or region.right > app.size.width:
            problems.append(f"{tag} #{button.id} outside the {app.size} viewport at {region}")
            continue
        try:
            geometry = app.screen.find_widget(button)
        except NoWidget:
            problems.append(f"{tag} #{button.id} is not rendered at all")
            continue
        visible = geometry.region.intersection(geometry.clip)
        if visible != region:
            problems.append(f"{tag} #{button.id} cropped to {visible} of {region}")
    return problems


def _modal_screens() -> list[tuple[str, object]]:
    return [
        ("add-person", AddPersonScreen()),
        ("search-form", SearchFormScreen(title="New search")),
        (
            "delivery",
            DeliveryFormScreen(
                initial={
                    "delivery_time": "09:00",
                    "timezone": "America/New_York",
                    "notify_on_no_results": False,
                }
            ),
        ),
    ]


def test_no_action_button_is_clipped_at_80_columns(tmp_path):
    """Regression for the 2026-08-09 layout defects and for the six-button
    Searches toolbar, whose last button sat entirely off the right edge."""

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            for index in range(25):
                app.controller.add_person(1000 + index, f"Person number {index}")
            for index in range(12):
                app.controller.add_search(1000, dict(SEARCH_FORM, name=f"Search {index}"))
            app.refresh_people()
            await pilot.pause()
            problems = _clipped_buttons(app, "dashboard")

            app.push_screen(SearchesScreen(app.controller, 1000, "Person number 0"))
            await pilot.pause()
            problems += _clipped_buttons(app, "searches")

            app.push_screen(StatusScreen(app.controller))
            await pilot.pause()
            problems += _clipped_buttons(app, "status")

            assert problems == [], problems

    run(scenario())


def test_modal_buttons_survive_short_terminals(tmp_path):
    """Regression for the 2026-08-11 clipping report: the Delivery dialog lost
    Save/Cancel below 23 terminal rows (entirely invisible at 18) and the
    Add-person dialog below 17. The dialog's fields region must absorb the
    squeeze so the button row never does."""

    async def scenario(height: int):
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, height)) as pilot:
            problems = []
            for tag, modal in _modal_screens():
                app.push_screen(modal)
                await pilot.pause()
                problems += _clipped_buttons(app, f"{tag}@80x{height}")
                app.pop_screen()
                await pilot.pause()
            assert problems == [], problems

    for height in (24, 20, 16):
        run(scenario(height))


def test_modal_dialogs_are_centered_not_pinned_top_left(tmp_path):
    """Textual's ModalScreen no longer centers children; the app CSS must."""

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            for tag, modal in _modal_screens():
                app.push_screen(modal)
                await pilot.pause()
                region = app.screen.query_one("#dialog").region
                assert region.x > 0 and region.y > 0, f"{tag} dialog pinned at {region}"
                app.pop_screen()
                await pilot.pause()

    run(scenario())


def test_escape_dismisses_each_modal(tmp_path):
    """When buttons were clipped there was no way out of a modal at all."""

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            for tag, modal in _modal_screens():
                app.push_screen(modal)
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is app.screen_stack[0], f"{tag} modal survived escape"

    run(scenario())


def test_add_person_rejects_exotic_unicode_digits_without_crashing(tmp_path):
    """"²".isdigit() is True but int("²") raises, which killed the handler."""

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#add-person")
            await pilot.pause()
            app.screen.query_one("#telegram_id", Input).value = "²"
            app.screen.query_one("#display_name", Input).value = "Peter"
            await pilot.click("#submit")
            await pilot.pause()
            assert isinstance(app.screen, AddPersonScreen), "modal must stay open"
            error = str(app.screen.query_one("#add-person-error", Label).render())
            assert "positive whole number" in error
            assert app.controller.list_people() == []

    run(scenario())


def test_search_form_actions_stay_visible_when_scrolled(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "Peter")
            app.refresh_people()
            await pilot.pause()
            screen = SearchFormScreen(title="New search")
            app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#search-form-fields").scroll_end(animate=False)
            await pilot.pause()
            for button_id in ("#submit", "#cancel"):
                region = screen.query_one(button_id, Button).region
                assert region.height >= 3, f"{button_id} collapsed to {region.height} rows"
                assert region.bottom <= app.size.height, f"{button_id} is below the fold"

    run(scenario())


def test_search_form_stays_open_and_keeps_input_on_a_missing_field(tmp_path):
    """Regression for the 2026-08-11 report: a blank required field closed the
    modal, discarded everything typed, and hid the error on the screen behind."""

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.add_person(111, "Peter")
            app.refresh_people()
            await pilot.pause()
            await pilot.click("#open-searches")
            await pilot.pause()

            await pilot.click("#new-search")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, SearchFormScreen)
            # Fill everything except property types (a required field).
            for field, value in SEARCH_FORM.items():
                if field in ("active", "property_types"):
                    continue
                form.query_one(f"#{field}", Input).value = value
            await pilot.click("#submit")
            await pilot.pause()

            assert app.screen is form, "the modal must stay open on a validation error"
            error = str(form.query_one("#search-form-error", Label).render())
            assert "property_types" in error
            assert form.query_one("#city", Input).value == "Austin", "input must be preserved"
            assert app.controller.list_searches(111) == []

            # Now fill the missing field and it saves and closes. The sleep keeps
            # this second click outside the first click's double-click window,
            # which would otherwise swallow the press.
            form.query_one("#property_types", Input).value = "apartment"
            await asyncio.sleep(0.6)
            await pilot.click("#submit")
            await pilot.pause()
            assert not isinstance(app.screen, SearchFormScreen)
            assert len(app.controller.list_searches(111)) == 1

    run(scenario())


def test_search_form_rejects_a_bad_state_without_closing(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.add_person(111, "Peter")
            app.refresh_people()
            await pilot.pause()
            await pilot.click("#open-searches")
            await pilot.pause()
            await pilot.click("#new-search")
            await pilot.pause()
            form = app.screen
            for field, value in SEARCH_FORM.items():
                if field == "active":
                    continue
                form.query_one(f"#{field}", Input).value = value
            # A city name is neither a state name nor a code, so it must be
            # refused in place ("Texas"/"New York" are now accepted and mapped).
            form.query_one("#state", Input).value = "Brooklyn"
            await pilot.click("#submit")
            await pilot.pause()
            assert app.screen is form, "an invalid state must keep the modal open"
            error = str(form.query_one("#search-form-error", Label).render())
            assert "state must be" in error
            assert app.controller.list_searches(111) == []

    run(scenario())


def test_search_form_accepts_a_full_state_name(tmp_path):
    """Typing the full state name must save, mapped to its 2-letter code."""

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.add_person(111, "Peter")
            app.refresh_people()
            await pilot.pause()
            await pilot.click("#open-searches")
            await pilot.pause()
            await pilot.click("#new-search")
            await pilot.pause()
            form = app.screen
            for field, value in SEARCH_FORM.items():
                if field == "active":
                    continue
                form.query_one(f"#{field}", Input).value = value
            form.query_one("#state", Input).value = "New York"
            await pilot.click("#submit")
            await pilot.pause()
            assert not isinstance(app.screen, SearchFormScreen), "a full state name must save"
            saved = app.controller.list_searches(111)
            assert len(saved) == 1 and saved[0].state == "NY"

    run(scenario())


def test_delivery_form_stays_open_on_a_bad_time(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.add_person(111, "Peter")
            app.refresh_people()
            await pilot.pause()
            await pilot.click("#open-searches")
            await pilot.pause()
            await pilot.click("#delivery-settings")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, DeliveryFormScreen)
            form.query_one("#delivery_time", Input).value = "9am"  # not HH:MM
            await pilot.click("#submit")
            await pilot.pause()
            assert app.screen is form, "a bad delivery time must keep the modal open"
            error = str(form.query_one("#delivery-form-error", Label).render())
            assert "HH:MM" in error or "24-hour" in error

    run(scenario())


async def _open_new_search_form(app, pilot):
    app.controller.add_person(111, "Peter")
    app.refresh_people()
    await pilot.pause()
    await pilot.click("#open-searches")
    await pilot.pause()
    await pilot.click("#new-search")
    await pilot.pause()
    return app.screen


def test_enter_submits_a_complete_search_form(tmp_path):
    """Regression for the 2026-08-11 report: a keyboard user pressed Enter to
    save, nothing happened, and Escape then discarded everything silently."""

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await _open_new_search_form(app, pilot)
            for field, value in SEARCH_FORM.items():
                if field == "active":
                    continue
                form.query_one(f"#{field}", Input).value = value
            form.query_one("#daily_cap", Input).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, SearchFormScreen), "Enter must save and close"
            assert len(app.controller.list_searches(111)) == 1
            status = str(app.screen.query_one("#searches-error", Label).render())
            assert "Saved search" in status, "the save must be confirmed on screen"

    run(scenario())


def test_enter_on_an_incomplete_form_shows_the_error_in_place(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await _open_new_search_form(app, pilot)
            form.query_one("#name", Input).value = "Only a name"
            form.query_one("#name", Input).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SearchFormScreen), "must stay open"
            error = str(form.query_one("#search-form-error", Label).render())
            assert "missing required field" in error
            assert form.query_one("#name", Input).value == "Only a name"

    run(scenario())


def test_escape_on_a_dirty_form_warns_once_then_discards(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await _open_new_search_form(app, pilot)
            form.query_one("#name", Input).value = "half-typed"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, SearchFormScreen), "first Esc must only warn"
            warning = str(form.query_one("#search-form-error", Label).render())
            assert "Unsaved changes" in warning
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SearchFormScreen), "second Esc discards"
            assert app.controller.list_searches(111) == []

    run(scenario())


def test_enter_submits_the_add_person_form(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#add-person")
            await pilot.pause()
            app.screen.query_one("#telegram_id", Input).value = "424242"
            app.screen.query_one("#display_name", Input).value = "Enter Person"
            app.screen.query_one("#display_name", Input).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, AddPersonScreen)
            assert app.controller.get_person(424242).display_name == "Enter Person"
            status = str(app.screen.query_one("#dashboard-error", Label).render())
            assert "Added" in status

    run(scenario())


def test_controller_rejects_a_duplicate_search_name(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    controller = TuiController(store)
    controller.add_person(111, "Peter")
    controller.add_search(111, SEARCH_FORM)
    try:
        controller.add_search(111, SEARCH_FORM)
    except Exception as exc:
        assert "already has a search" in str(exc)
    else:
        raise AssertionError("expected a duplicate-name error")


def test_controller_edits_persist_to_disk(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    controller = TuiController(store)
    controller.add_person(111, "Peter")
    controller.add_search(111, SEARCH_FORM)

    reopened = TuiController(Store(tmp_path))
    assert len(reopened.list_searches(111)) == 1
