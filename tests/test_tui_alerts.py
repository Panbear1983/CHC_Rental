"""Incremental dashboard actions must operate on shared runtime state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from textual.widgets import DataTable

import chc_rental.tui.controller as controller_module
from chc_rental.models import Allowlist, Profile
from chc_rental.sources.zillow import ZillowRentalAdapter
from chc_rental.store import Store
from chc_rental.tui.alerts import AlertsScreen, ConfirmActionScreen
from chc_rental.tui.app import OwnerDashboardApp
from chc_rental.tui.controller import TuiController

from tests.conftest import make_person, make_search
from tests.test_listing_events import BOUNDS, ImmediateClient, raw_listing


def run(coro):
    return asyncio.run(coro)


def prepared_alert_store(tmp_path) -> Store:
    store = Store(tmp_path)
    store.initialize()
    store.save_allowlist(
        Allowlist(
            people=[
                make_person(
                    111,
                    profile=Profile(searches=[make_search(name="Primary", state="TX")]),
                )
            ]
        )
    )
    store.migrate_config_v2()
    store.migrate_alert_ledger()
    return store


def test_controller_runs_a_real_shadow_pipeline_without_telegram(
    tmp_path, monkeypatch
):
    store = prepared_alert_store(tmp_path)
    (tmp_path / ".env").write_text("APIFY_TOKEN=fake-test-token\n")
    settings = store.load_settings().model_copy(
        update={"incremental_alerts_enabled": True, "zillow_enabled": True}
    )
    store.save_settings(settings)
    monkeypatch.setattr(
        controller_module,
        "ZillowRentalAdapter",
        lambda **kwargs: ZillowRentalAdapter(
            **kwargs, bounds_resolver=lambda query: BOUNDS
        ),
    )
    monkeypatch.setattr(
        controller_module,
        "ApifyClient",
        lambda **kwargs: ImmediateClient([raw_listing("a")]),
    )
    summary = TuiController(store).run_shadow_cycle(
        now_utc=datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    )
    assert summary["started"] == 1 and summary["records"] == 1
    assert summary["processing"]["observations"][0]["baseline_established"] is True
    assert store.event_store().outbox_records() == []


def test_dashboard_baseline_reset_requires_confirmation_and_is_audited(tmp_path):
    store = prepared_alert_store(tmp_path)
    query_id = "query-for-dashboard-test"
    now = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    store.event_store().sync_query_scopes(
        [{"query_id": query_id, "source": "zillow", "query_json": "{}"}],
        now_utc=now,
    )
    with store.event_store().connection() as connection:
        connection.execute(
            "UPDATE query_scopes SET baseline_state='established' WHERE query_id=?",
            (query_id,),
        )
        connection.commit()

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#open-status")
            await pilot.pause()
            await pilot.click("#status-open-alerts")
            await pilot.pause()
            assert isinstance(app.screen, AlertsScreen)
            table = app.screen.query_one("#query-health-table", DataTable)
            assert table.row_count == 1
            await pilot.click("#reset-baseline")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmActionScreen)
            assert store.event_store().query_baseline_states()[query_id] == "established"
            await pilot.click("#confirm-action")
            await pilot.pause()
            assert isinstance(app.screen, AlertsScreen)
            assert store.event_store().query_baseline_states()[query_id] == "reset_pending"
            with store.event_store().connection() as connection:
                audit = connection.execute(
                    "SELECT action, target_id FROM operator_audit"
                ).fetchone()
            assert tuple(audit) == ("reset_baseline", query_id)

    run(scenario())


def test_paid_shadow_button_stops_at_confirmation(tmp_path, monkeypatch):
    called = []

    async def scenario():
        app = OwnerDashboardApp(root=tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            monkeypatch.setattr(
                app.controller, "run_shadow_cycle", lambda: called.append(True)
            )
            await pilot.click("#open-status")
            await pilot.pause()
            await pilot.click("#status-open-alerts")
            await pilot.pause()
            await pilot.click("#run-shadow")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmActionScreen)
            assert called == []
            await pilot.click("#cancel-action")
            await pilot.pause()
            assert isinstance(app.screen, AlertsScreen)
            assert called == []

    run(scenario())
