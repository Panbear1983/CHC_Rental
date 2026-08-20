"""Headless TUI tests, including the crash and clipping regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from textual.color import Color
from textual.errors import NoWidget
from textual.widgets import Button, DataTable, Input, Label, Select, Switch, TabbedContent

from chc_rental.delivery_history import RoutineDeliveryItem
from chc_rental.models import Allowlist, DeliveryMode, Profile, Settings
from chc_rental.store import Store
from chc_rental.test_push import TestPushBlocked, TestPushPlan, TestPushResult
from chc_rental.tui.app import (
    AddPersonScreen,
    ConfigScreen,
    DeliveryFormScreen,
    MemberDetailsScreen,
    OwnerDashboardApp,
    SearchesScreen,
    SearchFormScreen,
    TestPushPreviewScreen,
)
from chc_rental.tui.controller import TuiController
from chc_rental.tui.alert_settings import IncrementalSettingsScreen
from chc_rental.tui.alerts import AlertsScreen
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
            assert sources.row_count == 1
            assert [str(sources.get_row_at(i)[0]) for i in range(1)] == ["zillow"]

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


def test_test_push_confirms_and_sends_only_the_selected_person(tmp_path):
    """The main-page action is selected-recipient only and visibly receipt-backed."""
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "First")
            app.controller.add_person(222, "Selected")
            app.refresh_people()
            await pilot.pause()
            table = app.query_one("#people-table", DataTable)
            table.move_cursor(row=1)
            await pilot.pause()

            prepared: list[int] = []
            sent: list[int] = []
            first_message = (
                "[Brooklyn 3bd 2ba] 1 Duffield St | Brooklyn | $6,800 | "
                "3 bed | 2 bath\n"
                "https://www.zillow.com/homedetails/1-Duffield-St/1_zpid/"
            )
            second_message = (
                "[Brooklyn 3bd 2ba] 2 Preview Ave | Brooklyn | $6,200 | "
                "3 bed | 2 bath\n"
                "https://www.zillow.com/homedetails/2-Preview-Ave/2_zpid/"
            )
            plan = TestPushPlan(
                telegram_id=222,
                display_name="Selected",
                preference_count=2,
                preference_fingerprint="fingerprint",
                messages=(first_message, second_message),
                listing_count=2,
                cache_date="2026-08-13",
            )

            def prepare(telegram_id):
                prepared.append(telegram_id)
                return plan

            def send(selected_plan):
                sent.append(selected_plan.telegram_id)
                return TestPushResult(
                    status="success",
                    telegram_id=222,
                    display_name="Selected",
                    preference_count=2,
                    parts_total=2,
                    parts_accepted=2,
                    message_ids=("987", "988"),
                    chat_ids=("222", "222"),
                )

            app.controller.prepare_test_push = prepare
            app.controller.send_test_push = send

            await pilot.click("#test-push")
            await asyncio.sleep(0.1)
            await pilot.pause()
            assert isinstance(app.screen, TestPushPreviewScreen)
            confirmation = str(
                app.screen.query_one("#test-push-preview-summary", Label).render()
            )
            assert "Selected" in confirmation and "222" in confirmation
            assert "2 active preferences" in confirmation
            assert "in 2 parts" in confirmation
            assert str(
                app.screen.query_one("#test-push-preview-part-1", Label).render()
            ) == first_message
            assert str(
                app.screen.query_one("#test-push-preview-part-2", Label).render()
            ) == second_message
            assert "Send exactly this" in str(
                app.screen.query_one("#confirm-action", Button).label
            )
            # A repeated shortcut while the modal is open cannot stage a second send.
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, TestPushPreviewScreen)
            await pilot.click("#cancel-action")
            await pilot.pause()
            assert sent == []

            await pilot.click("#test-push")
            await asyncio.sleep(0.1)
            await pilot.pause()
            await pilot.click("#confirm-action")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert prepared == [222, 222]
            assert sent == [222]
            status = str(app.query_one("#dashboard-error", Label).render())
            assert "receipt 987" in status

            app._test_push_active = True
            await pilot.click("#test-push")
            await pilot.pause()
            status = str(app.query_one("#dashboard-error", Label).render())
            assert "already in progress" in status
            assert sent == [222]

    run(scenario())


def test_test_push_preview_scrolls_and_keeps_send_controls_visible(tmp_path):
    long_message = "\n\n".join(
        f"[Filter] {index} Preview St | Brooklyn | $6,000 | 3 bed | 2 bath\n"
        f"https://www.zillow.com/homedetails/{index}_zpid/"
        for index in range(1, 16)
    )
    plan = TestPushPlan(
        telegram_id=111,
        display_name="Preview Recipient",
        preference_count=1,
        preference_fingerprint="preview",
        messages=(long_message,),
        listing_count=15,
        cache_date="2026-08-13",
    )

    async def scenario(height: int):
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, height)) as pilot:
            app.push_screen(TestPushPreviewScreen(plan))
            await pilot.pause()
            assert _clipped_buttons(app, f"test-push-preview@80x{height}") == []
            scroll = app.screen.query_one("#test-push-preview-scroll")
            assert scroll.virtual_size.height > scroll.size.height
            preview = str(
                app.screen.query_one("#test-push-preview-part-1", Label).render()
            )
            assert preview == long_message
            assert "https://www.zillow.com/homedetails/15_zpid/" in preview

    for height in (24, 16):
        run(scenario(height))


def test_repeat_test_push_uses_a_distinct_warning_confirmation(tmp_path):
    plan = TestPushPlan(
        telegram_id=111,
        display_name="Repeat Recipient",
        preference_count=1,
        preference_fingerprint="repeat",
        messages=("[Filter] 1 Repeat St\nhttps://www.zillow.com/1_zpid/",),
        listing_count=1,
        cache_date="2026-08-13",
        repeat_override=True,
    )

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(TestPushPreviewScreen(plan))
            await pilot.pause()
            summary = str(
                app.screen.query_one("#test-push-preview-summary", Label).render()
            )
            button = app.screen.query_one("#confirm-action", Button)
            assert "WARNING — REPEAT SEND" in summary
            assert "Resend seen listings" in str(button.label)
            assert button.variant == "error"

    run(scenario())


def test_test_push_preflight_failure_is_visible_and_never_opens_confirmation(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "Peter")
            app.refresh_people()

            def blocked(telegram_id):
                raise TestPushBlocked(
                    "Telegram bot token is invalid or revoked (401 Unauthorized)."
                )

            app.controller.prepare_test_push = blocked
            await pilot.click("#test-push")
            await asyncio.sleep(0.1)
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
            status = str(app.query_one("#dashboard-error", Label).render())
            assert "invalid or revoked" in status
            assert app._test_push_preflight_active is False

    run(scenario())


def test_test_push_is_yellow_pause_is_orange_and_remove_stays_red(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            test_push = app.query_one("#test-push", Button)
            assert test_push.styles.background == Color.parse("#FFD54F")
            assert test_push.styles.color == Color.parse("#000000")
            assert app.query_one("#toggle-person", Button).variant == "warning"
            assert app.query_one("#remove-person", Button).variant == "error"

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
        ("incremental-settings", IncrementalSettingsScreen(settings=Settings())),
        ("config", ConfigScreen(settings=Settings())),
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

            app.push_screen(AlertsScreen(app.controller))
            await pilot.pause()
            problems += _clipped_buttons(app, "alerts")

            assert problems == [], problems

    run(scenario())


def test_main_toolbar_has_nine_direct_controls_in_one_row(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            toolbar = app.query_one("#people-actions")
            buttons = list(toolbar.query(Button))
            assert [button.id for button in buttons] == [
                "add-person",
                "edit-person",
                "remove-person",
                "toggle-person",
                "set-push-time",
                "open-searches",
                "test-push",
                "open-config",
                "open-status",
            ]
            assert len({button.region.y for button in buttons}) == 1

    run(scenario())


def test_main_toolbar_buttons_fill_the_screen_width(tmp_path):
    async def scenario(width: int):
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(width, 24)) as pilot:
            await pilot.pause()
            toolbar = app.query_one("#people-actions")
            buttons = list(toolbar.query(Button))
            assert toolbar.region.x == 0
            assert toolbar.region.right == width
            assert buttons[0].region.x == 0
            assert buttons[-1].region.right == width
            assert all(
                left.region.right == right.region.x
                for left, right in zip(buttons, buttons[1:])
            )
            assert len({button.region.height for button in buttons}) == 1
            assert buttons[0].region.height == 3

    for width in (80, 120, 160):
        run(scenario(width))


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


def test_delivery_mode_and_quiet_hours_round_trip_through_dashboard(tmp_path):
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
            form.query_one("#delivery_mode", Select).value = "immediate"
            form.query_one("#timezone", Input).value = "Asia/Taipei"
            form.query_one("#quiet_hours_start", Input).value = "23:00"
            form.query_one("#quiet_hours_end", Input).value = "07:00"
            await pilot.click("#submit")
            await pilot.pause()
            profile = app.controller.get_person(111).profile
            assert profile.delivery_mode == DeliveryMode.IMMEDIATE
            assert profile.quiet_hours_start == "23:00"
            assert profile.quiet_hours_end == "07:00"
            title = str(app.screen.query_one("#searches-title", Label).render())
            assert "immediate" in title and "quiet 23:00-07:00" in title

    run(scenario())


def test_incremental_settings_control_writes_real_gates_and_limits(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.add_person(111, "Peter")
            app.controller.add_search(111, SEARCH_FORM)
            app.store.migrate_config_v2()
            app.store.migrate_alert_ledger()
            (tmp_path / ".env").write_text("APIFY_TOKEN=fake-test-token\n")
            await pilot.click("#open-status")
            await pilot.pause()
            await pilot.click("#alert-settings")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, IncrementalSettingsScreen)
            form.query_one("#zillow_enabled", Switch).value = True
            form.query_one("#zillow_terms_confirmed", Switch).value = True
            form.query_one("#incremental_alerts_enabled", Switch).value = True
            form.query_one("#zillow_interval", Input).value = "120"
            form.query_one("#zillow_daily_budget", Input).value = "7"
            form.query_one("#zillow_results_limit", Input).value = "30"
            form.query_one("#zillow_max_charge_usd", Input).value = "0.30"
            form.query_one("#incremental_monthly_budget_usd", Input).value = "12.50"
            form.query_one("#incremental_canary_telegram_ids", Input).value = "111"
            await pilot.click("#submit")
            await pilot.pause()
            saved = app.controller.settings()
            assert saved.incremental_alerts_enabled is True
            assert saved.zillow_enabled is True
            assert saved.zillow_actor == "maxcopell~zillow-scraper"
            assert saved.zillow_incremental_interval_minutes == 120
            assert saved.source_request_budget("zillow") == 7
            assert saved.zillow_results_limit == 30
            assert saved.zillow_max_charge_usd == 0.30
            assert saved.incremental_monthly_budget_usd == 12.50
            assert saved.incremental_canary_telegram_ids == [111]
            confirmation = str(
                app.screen.query_one("#incremental-action", Label).render()
            )
            assert "no scrape was started" in confirmation

    run(scenario())


def test_zillow_first_enable_requires_explicit_warning_confirmation(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    (tmp_path / ".env").write_text("APIFY_TOKEN=fake-test-token\n")
    controller = TuiController(store)
    try:
        controller.update_incremental_settings(
            incremental_alerts_enabled=False,
            zillow_enabled=True,
            zillow_terms_confirmed=False,
            zillow_actor="maxcopell~zillow-scraper",
            zillow_incremental_interval_minutes=180,
            zillow_daily_request_budget=5,
            zillow_results_limit=25,
            zillow_max_charge_usd=0.25,
            incremental_active_start="08:00",
            incremental_active_end="23:00",
            incremental_canary_telegram_ids=[],
            incremental_monthly_budget_usd=None,
        )
    except ValueError as exc:
        assert "confirm" in str(exc)
    else:
        raise AssertionError("first Zillow enable must require explicit confirmation")
    assert controller.settings().zillow_enabled is False


def test_dashboard_opens_incremental_alert_operations_screen(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#open-status")
            await pilot.pause()
            await pilot.click("#status-open-alerts")
            await pilot.pause()
            assert isinstance(app.screen, AlertsScreen)
            state = str(app.screen.query_one("#alerts-config", Label).render())
            assert "PAUSED" in state and "APIFY_TOKEN missing" in state
            assert "projection" in state and "breakers" in state

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


def test_edit_person_prefills_and_id_change_keeps_preferences_but_resets_state(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "Before")
            app.controller.add_search(111, SEARCH_FORM)
            before_profile = app.controller.get_person(111).profile.model_dump()
            app.store.mark_seen(111, "v3:old", search_name="Old", url="https://old")
            app.store.mark_seen(222, "v3:stale", search_name="Stale", url="https://stale")
            with app.store.edit_settings() as settings:
                settings.incremental_canary_telegram_ids = [111]
                settings.operator_alert_telegram_id = 111
            app.refresh_people()
            await pilot.pause()

            await pilot.click("#edit-person")
            await pilot.pause()
            assert isinstance(app.screen, MemberDetailsScreen)
            assert app.screen.query_one("#member-telegram-id", Input).value == "111"
            assert app.screen.query_one("#member-display-name", Input).value == "Before"
            app.screen.query_one("#member-telegram-id", Input).value = "222"
            app.screen.query_one("#member-display-name", Input).value = "After"
            await pilot.click("#save-member")
            await pilot.pause()

            people = app.controller.list_people()
            assert [(person.telegram_id, person.display_name) for person in people] == [
                (222, "After")
            ]
            assert people[0].profile.model_dump() == before_profile
            assert not app.store.seen_path(111).exists()
            assert not app.store.seen_path(222).exists()
            settings = app.controller.settings()
            assert settings.incremental_canary_telegram_ids == [222]
            assert settings.operator_alert_telegram_id == 111
            assert app.store.routine_journal_dir(111).exists()
            assert app.controller.routine_delivery_diary(222) == []
            status = str(app.query_one("#dashboard-error", Label).render())
            assert "empty routine diary" in status

    run(scenario())


def test_edit_person_rejects_duplicate_id_in_the_open_form(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "First")
            app.controller.add_person(222, "Second")
            app.refresh_people()
            await pilot.pause()
            await pilot.click("#edit-person")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, MemberDetailsScreen)
            form.query_one("#member-telegram-id", Input).value = "222"
            await pilot.click("#save-member")
            await pilot.pause()
            assert app.screen is form
            error = str(form.query_one("#member-details-error", Label).render())
            assert "already on the allowlist" in error
            assert [person.telegram_id for person in app.controller.list_people()] == [111, 222]

    run(scenario())


def test_member_edit_page_shows_grouped_routine_diary_and_exact_links(tmp_path):
    now = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "Diary Person")
            item = RoutineDeliveryItem(
                key="v3:ny:brooklyn::1%20duffield%20st:418",
                search_name="Brooklyn 3bd 2ba",
                url="https://www.zillow.com/homedetails/1-Duffield/1_zpid/",
                address="1 Duffield St",
                unit="418",
                city="Brooklyn",
                price=6800,
                beds=3,
                baths=2,
                source="zillow",
            )
            accepted = app.store.prepare_routine_delivery(
                111,
                display_name="Diary Person",
                timezone_name="UTC",
                local_day=now.date(),
                kind="listing",
                message_text=(
                    "[Brooklyn 3bd 2ba] 1 Duffield St | Brooklyn | $6,800 | "
                    "3 bed | 2 bath\n"
                    "https://www.zillow.com/homedetails/1-Duffield/1_zpid/"
                ),
                items=[item],
                now_utc=now,
            )
            app.store.transition_routine_delivery(
                111,
                now.date(),
                accepted.attempt_id,
                state="sending",
                now_utc=now,
            )
            app.store.transition_routine_delivery(
                111,
                now.date(),
                accepted.attempt_id,
                state="accepted",
                now_utc=now,
                telegram_message_id="801",
                chat_id="111",
            )
            uncertain = app.store.prepare_routine_delivery(
                111,
                display_name="Diary Person",
                timezone_name="UTC",
                local_day=now.date(),
                kind="notice",
                message_text="No new rentals matched your searches today.",
                items=[],
                now_utc=now + timedelta(minutes=1),
            )
            app.store.transition_routine_delivery(
                111,
                now.date(),
                uncertain.attempt_id,
                state="sending",
                now_utc=now + timedelta(minutes=1),
            )
            app.store.transition_routine_delivery(
                111,
                now.date(),
                uncertain.attempt_id,
                state="uncertain",
                now_utc=now + timedelta(minutes=1),
                error="response lost",
            )
            app.refresh_people()
            await pilot.pause()

            await pilot.click("#edit-person")
            await pilot.pause()
            assert isinstance(app.screen, MemberDetailsScreen)
            assert _clipped_buttons(app, "member-profile@80x24") == []
            tabs = app.screen.query_one("#member-details-tabs", TabbedContent)
            tabs.active = "member-diary-pane"
            await pilot.pause()

            days = app.screen.query_one("#routine-diary-days", DataTable)
            entries = app.screen.query_one("#routine-diary-entries", DataTable)
            assert days.row_count == 1
            assert [str(value) for value in days.get_row_at(0)] == [
                "2026-01-15",
                "1",
                "0",
                "1",
            ]
            assert entries.row_count == 2
            detail = str(
                app.screen.query_one("#routine-diary-detail", Label).render()
            )
            assert "Telegram receipt: 801" in detail
            assert "1 Duffield St" in detail
            assert "https://www.zillow.com/homedetails/1-Duffield/1_zpid/" in detail

            entries.move_cursor(row=1)
            await pilot.pause()
            warning = str(
                app.screen.query_one("#routine-diary-detail", Label).render()
            )
            assert "UNCERTAIN" in warning
            assert "may or may not have arrived" in warning
            back = app.screen.query_one("#member-details-back", Button)
            assert back.region.width > 0 and back.region.height == 3

    run(scenario())


def test_edit_and_remove_are_blocked_during_a_test_push(tmp_path):
    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "Busy")
            app.refresh_people()
            app._test_push_active = True
            await pilot.pause()
            await pilot.click("#edit-person")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
            assert "blocked" in str(app.query_one("#dashboard-error", Label).render())
            await asyncio.sleep(0.6)
            await pilot.click("#remove-person")
            await pilot.pause()
            assert app.controller.get_person(111).display_name == "Busy"
            assert "blocked" in str(app.query_one("#dashboard-error", Label).render())

    run(scenario())


def test_name_only_edit_preserves_seen_state(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    controller = TuiController(store)
    controller.add_person(111, "Before")
    store.mark_seen(111, "v3:keep", search_name="Keep", url="https://keep")
    outcome = controller.update_person(111, 111, "After")
    assert outcome.id_changed is False
    assert controller.get_person(111).display_name == "After"
    assert store.seen_keys(111) == {"v3:keep"}
    assert store.alert_db_path.exists() is False


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


def test_push_time_is_editable_from_the_main_page(tmp_path):
    """The push time now has a dedicated control on the people list, so owners
    no longer dig through Open searches -> Delivery to change when pushes go out."""
    from chc_rental.tui.app import PushTimeScreen

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "Peter")
            app.refresh_people()
            await pilot.pause()
            before = app.controller.get_person(111).profile
            await pilot.click("#set-push-time")
            await pilot.pause()
            assert isinstance(app.screen, PushTimeScreen), "main-page push-time control must open"
            app.screen.query_one("#delivery_time", Input).value = "07:45"
            app.screen.query_one("#timezone", Input).value = "America/Los_Angeles"
            await pilot.click("#submit")
            await pilot.pause()
            after = app.controller.get_person(111).profile
            assert after.delivery_time == "07:45"
            assert after.timezone == "America/Los_Angeles"
            # nothing else about delivery was disturbed
            assert after.delivery_mode == before.delivery_mode
            assert after.notify_on_no_results == before.notify_on_no_results
            status = str(app.screen.query_one("#dashboard-error", Label).render())
            assert "Push time" in status

    run(scenario())


def test_push_time_control_rejects_a_bad_time_in_place(tmp_path):
    from chc_rental.tui.app import PushTimeScreen

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "Peter")
            app.refresh_people()
            await pilot.pause()
            await pilot.click("#set-push-time")
            await pilot.pause()
            form = app.screen
            form.query_one("#delivery_time", Input).value = "7am"  # not HH:MM
            await pilot.click("#submit")
            await pilot.pause()
            assert app.screen is form, "a bad time must keep the dialog open"
            error = str(form.query_one("#push-time-error", Label).render())
            assert "HH:MM" in error or "24-hour" in error

    run(scenario())


# --- Config screen + hard-delete + before-scrape guardrail (2026-08-13) -------


def _config_env(tmp_path):
    (tmp_path / ".env").write_text("APIFY_TOKEN=faketoken1234567890\n", encoding="utf-8")


def test_config_screen_writes_daily_gates_and_round_trips(tmp_path):
    _config_env(tmp_path)

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.click("#open-config")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            form = app.screen
            form.query_one("#live_push_enabled", Switch).value = True
            form.query_one("#scrape_time", Input).value = "06:45"
            form.query_one("#scrape_timezone", Input).value = "America/Los_Angeles"
            form.query_one("#global_daily_request_budget", Input).value = "175"
            form.query_one("#operator_alert_telegram_id", Input).value = "424242"
            form.query_one("#zillow_enabled", Switch).value = True
            form.query_one("#zillow_terms_confirmed", Switch).value = True
            form.query_one("#zillow_daily_request_budget", Input).value = "9"
            await pilot.click("#submit")
            await pilot.pause()
            assert not isinstance(app.screen, ConfigScreen), "valid save closes the modal"
            s = app.controller.settings()
            assert s.live_push_enabled is True
            assert s.scrape_time == "06:45" and s.scrape_timezone == "America/Los_Angeles"
            assert s.global_daily_request_budget == 175
            assert s.operator_alert_telegram_id == 424242
            assert s.zillow_enabled is True
            assert s.source_request_budget("zillow") == 9
            # persists to disk for a fresh process
            reopened = TuiController(Store(tmp_path))
            assert reopened.settings().scrape_time == "06:45"

    run(scenario())


def test_config_first_zillow_enable_requires_terms(tmp_path):
    _config_env(tmp_path)
    store = Store(tmp_path)
    store.initialize()
    controller = TuiController(store)
    try:
        controller.update_run_settings(
            live_push_enabled=False, scrape_time="08:00", scrape_timezone="America/New_York",
            global_daily_request_budget=100, per_source_daily_request_budget=50,
            operator_alert_telegram_id=None,
            zillow_enabled=True, zillow_terms_confirmed=False,
            zillow_actor="maxcopell~zillow-scraper", zillow_daily_request_budget=5,
            zillow_results_limit=25, zillow_max_charge_usd=0.25, zillow_timeout_seconds=300,
        )
    except Exception as exc:
        assert "confirm" in str(exc).lower()
    else:
        raise AssertionError("enabling Zillow without confirming terms must fail")
    assert controller.settings().zillow_enabled is False


def test_config_bad_scrape_time_keeps_modal_open(tmp_path):
    _config_env(tmp_path)

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.click("#open-config")
            await pilot.pause()
            form = app.screen
            form.query_one("#scrape_time", Input).value = "7am"
            await pilot.click("#submit")
            await pilot.pause()
            assert app.screen is form, "an invalid scrape time must keep the modal open"
            error = str(form.query_one("#config-error", Label).render())
            assert "HH:MM" in error or "24-hour" in error
            assert app.controller.settings().scrape_time != "7am"

    run(scenario())


def test_config_blank_operator_id_disables_alerts_negative_rejected(tmp_path):
    _config_env(tmp_path)

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.click("#open-config")
            await pilot.pause()
            form = app.screen
            form.query_one("#operator_alert_telegram_id", Input).value = ""
            await pilot.click("#submit")
            await pilot.pause()
            assert app.controller.settings().operator_alert_telegram_id is None
            # negative id rejected in place
            await asyncio.sleep(0.6)
            await pilot.click("#open-config")
            await pilot.pause()
            form = app.screen
            form.query_one("#operator_alert_telegram_id", Input).value = "-5"
            await pilot.click("#submit")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen), "negative id must keep the modal open"

    run(scenario())


def test_remove_person_button_purges_operational_state_after_confirm(tmp_path):
    from chc_rental.tui.alerts import ConfirmActionScreen

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "Gone")
            app.controller.add_search(111, SEARCH_FORM)
            app.store.mark_seen(111, "v3:gone", search_name="Gone", url="https://gone")
            with app.store.edit_settings() as settings:
                settings.incremental_canary_telegram_ids = [111]
                settings.operator_alert_telegram_id = 111
            app.refresh_people()
            await pilot.pause()
            app.query_one("#people-table", DataTable).move_cursor(row=0)
            await pilot.pause()
            await pilot.click("#remove-person")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmActionScreen)
            # cancel keeps the person
            await pilot.click("#cancel-action")
            await pilot.pause()
            assert any(p.telegram_id == 111 for p in app.controller.list_people())
            # confirm deletes them
            app.query_one("#people-table", DataTable).move_cursor(row=0)
            await asyncio.sleep(0.6)
            await pilot.click("#remove-person")
            await pilot.pause()
            await pilot.click("#confirm-action")
            await pilot.pause()
            assert app.controller.list_people() == []
            assert not app.store.seen_path(111).exists()
            settings = app.controller.settings()
            assert settings.incremental_canary_telegram_ids == []
            assert settings.operator_alert_telegram_id == 111
            assert app.store.alert_db_path.exists() is False

    run(scenario())


def test_delivery_form_sections_daily_and_incremental(tmp_path):
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
            labels = [str(w.render()) for w in form.query(Label)]
            assert any("Daily push" in t for t in labels)
            assert any("Incremental alerts (inactive" in t for t in labels)
            # the daily-relevant no-match toggle is still present and reachable
            assert form.query_one("#notify_on_no_results", Switch) is not None

    run(scenario())


def test_config_warns_when_a_person_pushes_before_scrape(tmp_path):
    _config_env(tmp_path)

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.add_person(111, "EarlyBird")
            app.controller.update_delivery(
                111, delivery_time="06:00", timezone_name="America/New_York",
                delivery_mode="daily", quiet_hours_start=None, quiet_hours_end=None,
                notify_on_no_results=False,
            )
            app.refresh_people()
            await pilot.pause()
            await pilot.click("#open-config")
            await pilot.pause()
            app.screen.query_one("#scrape_time", Input).value = "10:00"
            await pilot.click("#submit")
            await pilot.pause()
            msg = str(app.screen.query_one("#dashboard-error", Label).render())
            assert "⚠" in msg and "EarlyBird" in msg

    run(scenario())
