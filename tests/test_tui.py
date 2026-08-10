"""Headless TUI tests, including the crash and clipping regressions."""

from __future__ import annotations

import asyncio

from textual.widgets import Button, DataTable, Input, Label, Switch

from chc_rental.models import Allowlist, Profile
from chc_rental.store import Store
from chc_rental.tui.app import OwnerDashboardApp, SearchesScreen, SearchFormScreen
from chc_rental.tui.controller import TuiController
from chc_rental.tui.status import StatusScreen

from tests.conftest import make_person, make_search

SEARCH_FORM = {
    "name": "Downtown 2BR",
    "city": "Taipei",
    "district": "Da'an",
    "price_min": "20000",
    "price_max": "35000",
    "property_types": "apartment, condo",
    "bed_min": "2",
    "bed_max": "3",
    "bath_min": "1",
    "bath_max": "2",
    "sqft_min": "",
    "sqft_max": "",
    "required_features": "elevator",
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
            assert searches[0].city == "Taipei"

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
    problems = []
    for button in app.screen.query(Button):
        region = button.region
        if region.width == 0 or region.height < 3:
            problems.append(f"{tag} #{button.id} collapsed to {region}")
        elif region.bottom > app.size.height or region.right > app.size.width:
            problems.append(f"{tag} #{button.id} outside the {app.size} viewport at {region}")
    return problems


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
