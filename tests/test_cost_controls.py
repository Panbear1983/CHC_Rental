"""Monthly cost, source breaker, scheduler isolation, and backup recovery."""

from __future__ import annotations

from datetime import timedelta

import pytest

from chc_rental.delivery_worker import DeliveryWorker
from chc_rental.event_store import AlertStoreError
from chc_rental.incremental import IncrementalCollector
from chc_rental.operations import incremental_cost_status
from chc_rental.outbox import process_incremental_report
from chc_rental.scheduler import IncrementalScheduler
from chc_rental.sources.apify import ApifyRunState
from chc_rental.sources.base import SourceAuthError
from chc_rental.sources.zillow import ZillowRentalAdapter

from tests.test_apify_run_lifecycle import BOUNDS, FakeClient, NOW, prepared_store
from tests.test_delivery_worker import RecordingSender, shadow_for_peter
from tests.test_listing_events import raw_listing


def make_collector(store, settings, client):
    return IncrementalCollector(
        store,
        settings=settings,
        adapter=ZillowRentalAdapter(
            token="SECRET", results_limit=25, bounds_resolver=lambda query: BOUNDS
        ),
        client=client,
        worker_id="cost-control-test",
    )


class AuthThenSuccessClient(FakeClient):
    def __init__(self):
        super().__init__(start_status="SUCCEEDED")
        self.reject_auth = True

    def start_actor(self, actor, payload, *, max_total_charge_usd):
        self.start_calls += 1
        if self.reject_auth:
            raise SourceAuthError("test rejected token")
        return ApifyRunState(
            f"remote-{self.start_calls}", "SUCCEEDED", "dataset-1", 0.04, "done"
        )


class CostClient(FakeClient):
    def start_actor(self, actor, payload, *, max_total_charge_usd):
        self.start_calls += 1
        return ApifyRunState(
            f"remote-{self.start_calls}", "SUCCEEDED", "dataset-1", 0.06, "done"
        )


def test_auth_failure_opens_breaker_immediately_and_half_open_success_recovers(store):
    settings = prepared_store(store).model_copy(
        update={
            "zillow_incremental_interval_minutes": 1,
            "incremental_source_breaker_failures": 3,
            "incremental_source_breaker_cooldown_minutes": 30,
        }
    )
    client = AuthThenSuccessClient()
    first = make_collector(store, settings, client).cycle(now_utc=NOW)
    assert first.collections[0].status == "auth_failed"
    breaker = store.event_store().source_breakers()[0]
    assert breaker.state == "open" and breaker.consecutive_failures == 1

    blocked = make_collector(store, settings, client).cycle(
        now_utc=NOW + timedelta(minutes=2)
    )
    assert any("circuit breaker" in item for item in blocked.warnings)
    assert client.start_calls == 1

    store.event_store().acknowledge_source_alert("zillow", now_utc=NOW)
    client.reject_auth = False
    recovered = make_collector(store, settings, client).cycle(
        now_utc=NOW + timedelta(minutes=31)
    )
    process_incremental_report(
        store, recovered, now_utc=NOW + timedelta(minutes=31)
    )
    breaker = store.event_store().source_breakers()[0]
    assert recovered.started == 1
    assert breaker.state == "closed" and breaker.consecutive_failures == 0
    assert breaker.pending_alert == "recovered"


def test_monthly_soft_budget_blocks_new_paid_starts_but_not_completed_processing(store):
    settings = prepared_store(store).model_copy(
        update={
            "zillow_incremental_interval_minutes": 1,
            "incremental_monthly_budget_usd": 0.05,
        }
    )
    client = CostClient(start_status="SUCCEEDED")
    first = make_collector(store, settings, client).cycle(now_utc=NOW)
    process_incremental_report(store, first, now_utc=NOW)
    assert client.start_calls == 1
    later = make_collector(store, settings, client).cycle(
        now_utc=NOW + timedelta(minutes=2)
    )
    assert later.started == 0 and client.start_calls == 1
    assert any("monthly soft budget" in item for item in later.warnings)
    cost = store.event_store().monthly_source_cost("zillow", now_utc=NOW)
    assert cost["known_cost_usd"] == pytest.approx(0.06)
    status = incremental_cost_status(store, settings, now_utc=NOW)
    assert status["monthly_budget_reached"] is True
    assert status["automatic_cadence_increase_allowed"] is False


def test_unknown_cost_prevents_automatic_cadence_increase_claim(store):
    settings = prepared_store(store)
    client = FakeClient(start_status="SUCCEEDED")  # cost is deliberately unknown
    first = make_collector(store, settings, client).cycle(now_utc=NOW)
    process_incremental_report(store, first, now_utc=NOW)
    status = incremental_cost_status(store, settings, now_utc=NOW)
    assert status["cost_unknown"] is True
    assert status["automatic_cadence_increase_allowed"] is False
    assert status["projected_monthly_cost_usd"] is None


def test_projection_respects_the_global_daily_start_cap(store):
    settings = prepared_store(store).model_copy(
        update={
            "global_daily_request_budget": 1,
            "source_daily_request_budgets": {"zillow": 50},
        }
    )
    make_collector(store, settings, FakeClient(start_status="SUCCEEDED")).cycle(
        now_utc=NOW
    )
    status = incremental_cost_status(store, settings, now_utc=NOW)
    assert status["uncapped_daily_runs"] > 1
    assert status["capped_daily_runs"] == 1


def test_daily_sqlite_backup_and_disposable_restore_drill(store, tmp_path):
    store.migrate_alert_ledger()
    backup = store.event_store().create_daily_backup(now_utc=NOW)
    assert backup.exists() and oct(backup.stat().st_mode)[-3:] == "600"
    assert store.event_store().create_daily_backup(now_utc=NOW) == backup
    drill = store.event_store().restore_drill(backup)
    assert drill["integrity"] == "ok"
    assert drill["live_database_untouched"] is True
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(AlertStoreError):
        store.event_store().restore_drill(corrupt)


class ExplodingCollector:
    def cycle(self, *, now_utc):
        raise RuntimeError("simulated source crash")


def test_source_cycle_crash_does_not_stop_already_queued_delivery(store):
    _, row = shadow_for_peter(store)
    sender = RecordingSender()
    report = IncrementalScheduler(
        store,
        collector=ExplodingCollector(),
        sender=sender,
    ).tick(now_utc=NOW + timedelta(hours=3))
    assert any("source cycle failed" in item for item in report.warnings)
    assert report.delivery["sent"] == 1
    assert store.event_store().outbox_records()[0].status == "sent"
    assert sender.sent[0][0] == row.telegram_id


def test_budget_exhaustion_stops_collection_but_not_queued_delivery(store):
    settings, row = shadow_for_peter(store)
    settings = settings.model_copy(update={"incremental_monthly_budget_usd": 0.01})
    store.save_settings(settings)
    client = FakeClient(start_status="SUCCEEDED")
    collector = make_collector(store, settings, client)
    sender = RecordingSender()
    report = IncrementalScheduler(
        store, collector=collector, sender=sender
    ).tick(now_utc=NOW + timedelta(hours=6))
    assert any("monthly soft budget" in item for item in report.source["warnings"])
    assert client.start_calls == 0
    assert report.delivery["sent"] == 1
    assert store.event_store().outbox_records()[0].status == "sent"
    assert sender.sent[0][0] == row.telegram_id


def test_scheduler_tick_skips_when_daily_workflow_holds_whole_run_lock(store):
    store.migrate_alert_ledger()
    scheduler = IncrementalScheduler(store, collector=None, sender=None)
    with store.try_run_lock() as acquired:
        assert acquired is True
        report = scheduler.tick(now_utc=NOW)
    assert report.skipped_locked is True


def test_breaker_owner_alerts_once_then_reports_recovery(store):
    store.migrate_alert_ledger()
    events = store.event_store()
    events.record_source_failure(
        "zillow",
        now_utc=NOW,
        error_class="auth_failed",
        error_message="rejected",
        threshold=1,
        cooldown_minutes=30,
        immediate_open=True,
    )
    alerts = []
    scheduler = IncrementalScheduler(
        store,
        collector=None,
        sender=None,
        operator_alert=lambda text: alerts.append(text) or True,
    )
    scheduler.tick(now_utc=NOW)
    scheduler.tick(now_utc=NOW + timedelta(minutes=1))
    assert len(alerts) == 1 and "breaker opened" in alerts[0]

    events.record_source_success("zillow", now_utc=NOW + timedelta(minutes=2))
    scheduler.tick(now_utc=NOW + timedelta(minutes=2))
    scheduler.tick(now_utc=NOW + timedelta(minutes=3))
    assert len(alerts) == 2 and "recovered" in alerts[1]


def test_failure_streak_alerts_once_before_breaker_threshold_then_recovers(store):
    store.migrate_alert_ledger()
    events = store.event_store()
    events.record_source_failure(
        "zillow",
        now_utc=NOW,
        error_class="poll_failed",
        error_message="temporary",
        threshold=3,
        cooldown_minutes=30,
    )
    alerts = []
    scheduler = IncrementalScheduler(
        store,
        collector=None,
        sender=None,
        operator_alert=lambda text: alerts.append(text) or True,
    )
    scheduler.tick(now_utc=NOW)
    scheduler.tick(now_utc=NOW + timedelta(minutes=1))
    assert len(alerts) == 1 and "failure started" in alerts[0]
    assert events.source_breakers()[0].state == "closed"

    events.record_source_success("zillow", now_utc=NOW + timedelta(minutes=2))
    scheduler.tick(now_utc=NOW + timedelta(minutes=2))
    assert len(alerts) == 2 and "recovered" in alerts[1]


def test_opened_breaker_stops_remaining_paid_starts_in_same_cycle(store):
    settings = prepared_store(store, searches=2).model_copy(
        update={"incremental_max_new_starts_per_cycle": 2}
    )
    client = AuthThenSuccessClient()
    report = make_collector(store, settings, client).cycle(now_utc=NOW)
    assert report.started == 1
    assert client.start_calls == 1


def test_retention_prunes_completed_history_but_keeps_active_query_scope(store):
    _, row = shadow_for_peter(store)
    DeliveryWorker(store, sender=RecordingSender()).run(
        now_utc=NOW + timedelta(hours=3),
        telegram_ids={111},
        outbox_id=row.outbox_id,
    )
    with store.event_store().connection() as connection:
        assert connection.execute("SELECT count(*) FROM source_runs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM delivery_receipts").fetchone()[0] == 1

    removed = store.event_store().prune_history(
        before_utc=NOW + timedelta(days=200)
    )

    assert removed["delivery_receipts"] == 1
    assert removed["outbox"] == 1
    assert removed["source_runs"] == 2
    with store.event_store().connection() as connection:
        assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM listing_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM listing_versions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM listing_identities").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM query_scopes").fetchone()[0] == 1


# --- the refund must never credit more than was reserved ----------------------
#
# Refunding an auth-rejected reservation is what stops a bad token eating the
# day's budget. But an over-credit is far worse than the bug it fixes: it would
# hand back budget that was really spent, and the ceiling is the only thing
# standing between this project and an unbounded Apify bill.


def test_refund_never_credits_more_than_was_reserved(store):
    from datetime import date

    day = date(2026, 1, 15)
    assert store.reserve_request(day, "zillow", per_source_limit=5, global_limit=100)
    assert store.quota_used(day, "zillow") == 1

    assert store.refund_request(day, "zillow") is True
    assert store.quota_used(day, "zillow") == 0

    # Every further refund is a no-op: there is nothing left to give back.
    for _ in range(5):
        assert store.refund_request(day, "zillow") is False
    assert store.quota_used(day, "zillow") == 0

    # An unknown source cannot manufacture credit either.
    assert store.refund_request(day, "never-reserved") is False
    assert store.quota_used(day, "never-reserved") == 0


def test_repeated_auth_failures_leave_the_ledger_where_they_found_it(store):
    """The 2026-08-20 shape: a ten-minute job against a rejected credential."""
    from datetime import datetime, timezone

    from chc_rental.fetch import fetch_daily
    from chc_rental.models import Allowlist, Profile
    from chc_rental.sources.base import SourceAuthError

    from tests.conftest import make_person, make_search

    store.save_allowlist(
        Allowlist(
            people=[make_person(111, profile=Profile(searches=[make_search(state="TX")]))]
        )
    )

    class Rejecting:
        name = "zillow"
        calls = 0

        def fetch_page(self, query, *, offset):
            type(self).calls += 1
            raise SourceAuthError("HTTP 403: Monthly usage hard limit exceeded")

    now = datetime(2026, 1, 15, 13, 5, tzinfo=timezone.utc)
    adapter = Rejecting()
    for minute in range(0, 60, 10):
        fetch_daily(store, adapter, now_utc=now.replace(minute=minute))

    assert store.quota_used(now.date(), "zillow") == 0, (
        "an hour of rejected requests must leave the day's budget intact"
    )
    assert Rejecting.calls == 1, "and must only actually ask the source once"
