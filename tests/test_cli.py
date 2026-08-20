"""CLI operator-alert behavior that spans fetch and delivery summaries."""

from datetime import datetime, timedelta, timezone
import json

import chc_rental.cli as cli
from chc_rental.fetch import scrape_day
from chc_rental.models import Allowlist, Profile, Settings
from chc_rental.pipeline import PipelineResult
from chc_rental.store import Store

from tests.conftest import make_listing, make_person, make_search


def truncated_fetch():
    return {
        "sources": [
            {
                "source": "rentcast",
                "truncated": True,
                "errors": [],
            }
        ]
    }


def test_problem_messages_include_incomplete_source_and_delivery_failures():
    problems = cli._problem_messages(
        truncated_fetch(), {"failed": 2, "no_results_failed": 1}
    )
    assert problems == [
        "rentcast fetch was truncated",
        "2 push(es) failed to send",
        "1 no-results notice(s) failed to send",
    ]


def test_replayed_cached_problem_alerts_once_per_streak(store, monkeypatch):
    sent = []
    monkeypatch.setattr(
        cli, "_alert_operator", lambda store, env_file, text: sent.append(text)
    )
    result = PipelineResult(summary={"delivery": "live", "failed": 0})

    cli._alert_on_problems(store, ".env", result, truncated_fetch())
    assert sent == ["rentcast fetch was truncated"]

    store.record_run(
        datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
        {"kind": "run", "fetch": truncated_fetch(), "summary": result.summary},
    )
    cli._alert_on_problems(store, ".env", result, truncated_fetch())
    assert sent == ["rentcast fetch was truncated"]

    changed = {"sources": [{"source": "rentcast", "errors": ["HTTP 500"]}]}
    cli._alert_on_problems(store, ".env", result, changed)
    assert sent[-1] == "rentcast fetch errors: HTTP 500"


def test_daily_delivery_uses_saved_cache_without_constructing_source_adapter(
    store, monkeypatch, capsys
):
    person = make_person(
        111,
        profile=Profile(
            delivery_time="09:00",
            timezone="America/New_York",
            searches=[make_search(state="TX")],
        ),
    )
    store.save_allowlist(Allowlist(people=[person]))
    settings = Settings(
        scrape_time="08:00",
        scrape_timezone="America/New_York",
        zillow_enabled=True,
    )
    store.save_settings(settings)
    now = datetime(2026, 1, 15, 20, tzinfo=timezone.utc)
    day = scrape_day(settings, now)
    store.cache_raw(
        day,
        "zillow",
        [make_listing(source="zillow", state="TX")],
        metadata={
            "query_scope": [{"city": "austin", "state": "TX"}],
            "scrape_day": day.isoformat(),
            "scrape_timezone": settings.scrape_timezone,
            "queries_planned": 1,
            "queries_completed": 1,
        },
    )
    monkeypatch.setattr(
        cli,
        "configured_adapters",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("delivery-only path must not construct an adapter")
        ),
    )
    monkeypatch.setattr(
        cli,
        "build_sender",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("dry delivery check must not construct Telegram sender")
        ),
    )

    code = cli.main(
        [
            "--root",
            str(store.root),
            "deliver",
            "--now",
            now.isoformat(),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "planned_pushes\": 1" in output


def test_daily_scrape_never_constructs_telegram_sender(store, monkeypatch, capsys):
    store.save_allowlist(
        Allowlist(
            people=[
                make_person(111, profile=Profile(searches=[make_search(state="TX")]))
            ]
        )
    )
    adapter = type(
        "OnePage",
        (),
        {
            "name": "zillow",
            "fetch_page": lambda self, query, offset: (
                [make_listing(source="zillow", state="TX")],
                False,
            ),
        },
    )()
    monkeypatch.setattr(cli, "configured_adapters", lambda *args, **kwargs: ([adapter], []))
    monkeypatch.setattr(
        cli,
        "build_sender",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("scrape-only path must never construct Telegram sender")
        ),
    )
    now = datetime(2026, 1, 15, 20, tzinfo=timezone.utc)
    code = cli.main(
        ["--root", str(store.root), "scrape", "--now", now.isoformat()]
    )
    assert code == 0
    assert '"scrape": "fetched"' in capsys.readouterr().out


def test_alert_status_is_read_only_and_reports_pending_migrations(tmp_path, capsys):
    assert cli.main(["--root", str(tmp_path), "alerts", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["ready"] is False
    assert payload["ledger"]["pending_versions"] == [1, 2, 3, 4, 5, 6, 7]
    assert payload["incremental"]["health"] is None
    assert payload["incremental"]["token_ready"] is False
    assert (tmp_path / "state" / "alerts.sqlite3").exists() is False


def test_alert_migration_apply_prepares_config_and_ledger(tmp_path, capsys):
    assert cli.main(["--root", str(tmp_path), "alerts", "migrate", "--apply"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["ready"] is True
    assert payload["ledger"]["ready"] is True
    assert (tmp_path / "state" / "alerts.sqlite3").exists()


def test_alert_fixture_cycle_is_offline_unmetered_and_never_sends(tmp_path, capsys):
    store = Store(tmp_path)
    store.initialize()
    store.save_allowlist(
        Allowlist(
            people=[
                make_person(
                    111,
                    profile=Profile(searches=[make_search(state="TX")]),
                )
            ]
        )
    )
    store.migrate_config_v2()
    store.migrate_alert_ledger()
    fixture = tmp_path / "zillow-items.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "zpid": "fixture-1",
                    "detailUrl": "/homedetails/fixture-1_zpid/",
                    "statusType": "FOR_RENT",
                    "addressStreet": "100 Main St",
                    "addressCity": "Austin",
                    "addressState": "TX",
                    "addressZipcode": "78701",
                    "unformattedPrice": 2200,
                    "beds": 2,
                    "baths": 1.5,
                    "area": 900,
                    "homeType": "APARTMENT",
                }
            ]
        ),
        encoding="utf-8",
    )
    code = cli.main(
        [
            "--root",
            str(tmp_path),
            "alerts",
            "cycle",
            "--fixture",
            str(fixture),
            "--now",
            "2026-01-15T15:00:00Z",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["records"] == 1
    assert payload["collections"][0]["status"] == "succeeded"
    assert store.quota_used(datetime(2026, 1, 15).date(), "zillow") == 0
    assert store.seen_keys(111) == set()


# --- unattended failure reporting -------------------------------------------
#
# The scheduled job runs `scrape` then `deliver`, never `run`. On 2026-08-20 the
# Apify free tier hit its cap; every scrape 403'd and every delivery was skipped,
# and nobody was told for a full day because the only alert lived in `run`.


def _alerting_store(store, monkeypatch):
    """A live-ish store whose operator alerts are captured instead of sent."""
    sent = []
    monkeypatch.setattr(
        cli, "_alert_operator", lambda store, env_file, text: sent.append(text)
    )
    store.save_allowlist(
        Allowlist(
            people=[make_person(111, profile=Profile(searches=[make_search(state="TX")]))]
        )
    )
    store.save_settings(
        Settings(
            scrape_time="08:00",
            scrape_timezone="America/New_York",
            zillow_enabled=True,
            operator_alert_telegram_id=999,
        )
    )
    return sent


def _scrape(store, monkeypatch, adapter, now):
    monkeypatch.setattr(cli, "configured_adapters", lambda *a, **k: ([adapter], []))
    return cli.main(["--root", str(store.root), "scrape", "--now", now.isoformat()])


class _FailingAdapter:
    """Refuses every page the way an exhausted Apify subscription does."""

    name = "zillow"

    def __init__(self, message="Monthly usage hard limit exceeded"):
        self.message = message

    def fetch_page(self, query, *, offset):
        from chc_rental.sources.base import SourceAuthError

        raise SourceAuthError(f"apify rejected the Zillow source token (HTTP 403: {self.message})")


def test_failed_scrape_alerts_the_operator_once_per_streak(store, monkeypatch):
    sent = _alerting_store(store, monkeypatch)
    adapter = _FailingAdapter()

    _scrape(store, monkeypatch, adapter, datetime(2026, 1, 15, 20, tzinfo=timezone.utc))
    assert len(sent) == 1
    assert "scrape failed" in sent[0] and "403" in sent[0]

    # Ten minutes later the same failure must not page again, and the day
    # breaker means the source is not even re-attempted.
    _scrape(store, monkeypatch, adapter, datetime(2026, 1, 15, 20, 10, tzinfo=timezone.utc))
    assert len(sent) == 1, f"a failure streak must page once, got {sent}"

    # An hour of ten-minute ticks — the shape of a real morning — stays silent.
    tick = datetime(2026, 1, 15, 20, 10, tzinfo=timezone.utc)
    for _ in range(6):
        tick += timedelta(minutes=10)
        _scrape(store, monkeypatch, adapter, tick)
    assert len(sent) == 1, f"the breaker must not page per tick, got {sent}"

    # Tomorrow is a fresh day: the breaker clears and a new failure pages again.
    _scrape(
        store,
        monkeypatch,
        _FailingAdapter("token revoked"),
        datetime(2026, 1, 16, 20, tzinfo=timezone.utc),
    )
    assert len(sent) == 2
    assert "token revoked" in sent[1]


def test_scrape_before_its_time_never_alerts(store, monkeypatch):
    sent = _alerting_store(store, monkeypatch)
    # 06:00 America/New_York, before the 08:00 scrape gate.
    _scrape(
        store,
        monkeypatch,
        _FailingAdapter(),
        datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
    )
    assert sent == [], "the overnight pre-scrape window is not a fault"


def test_delivery_does_not_double_page_for_a_failure_the_scrape_reported(
    store, monkeypatch
):
    sent = _alerting_store(store, monkeypatch)
    now = datetime(2026, 1, 15, 20, tzinfo=timezone.utc)
    _scrape(store, monkeypatch, _FailingAdapter(), now)
    assert len(sent) == 1

    monkeypatch.setattr(cli, "build_sender", lambda *a, **k: None)
    cli.main(["--root", str(store.root), "deliver", "--now", now.isoformat()])
    assert len(sent) == 1, f"one root cause must page once, got {sent}"


def test_delivery_alerts_when_a_healthy_scrape_still_left_no_usable_pool(
    store, monkeypatch
):
    sent = _alerting_store(store, monkeypatch)
    settings = store.load_settings()
    now = datetime(2026, 1, 15, 20, tzinfo=timezone.utc)
    day = scrape_day(settings, now)

    class OnePage:
        name = "zillow"

        def fetch_page(self, query, *, offset):
            return [make_listing(source="zillow", state="TX")], False

    _scrape(store, monkeypatch, OnePage(), now)
    assert sent == []

    # The scrape reported success, but the pool no longer covers what delivery
    # needs — e.g. a watched city was added between the two commands.
    store.cache_metadata_path(day, "zillow").unlink()
    monkeypatch.setattr(cli, "build_sender", lambda *a, **k: None)
    cli.main(["--root", str(store.root), "deliver", "--now", now.isoformat()])

    assert len(sent) == 1
    assert "no usable listing pool" in sent[0]


# --- honest degradation ------------------------------------------------------
#
# Chun H. has notify_on_no_results enabled precisely so an empty day is not
# silent. On 2026-08-20 the delivery path returned before planning, so the one
# person who asked to hear about empty days heard nothing at all.


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, *, telegram_id, text):
        from chc_rental.notify.telegram import TelegramReceipt

        self.sent.append((telegram_id, text))
        return TelegramReceipt(message_id=str(100 + len(self.sent)), chat_id=str(telegram_id))


def _outage_store(store, monkeypatch, sender):
    monkeypatch.setattr(cli, "_alert_operator", lambda *a, **k: True)
    monkeypatch.setattr(cli, "build_sender", lambda *a, **k: sender)
    store.save_allowlist(
        Allowlist(
            people=[
                # wants to hear about empty days
                make_person(
                    111,
                    profile=Profile(
                        delivery_time="07:00",
                        timezone="America/New_York",
                        notify_on_no_results=True,
                        searches=[make_search(state="TX")],
                    ),
                ),
                # does not
                make_person(
                    222,
                    profile=Profile(
                        delivery_time="07:00",
                        timezone="America/New_York",
                        notify_on_no_results=False,
                        searches=[make_search(state="TX")],
                    ),
                ),
            ]
        )
    )
    store.save_settings(
        Settings(
            scrape_time="06:00",
            scrape_timezone="America/New_York",
            zillow_enabled=True,
            live_push_enabled=True,
            operator_alert_telegram_id=999,
        )
    )


def _fail_scrape_then_deliver(store, monkeypatch, now):
    monkeypatch.setattr(
        cli, "configured_adapters", lambda *a, **k: ([_FailingAdapter()], [])
    )
    cli.main(["--root", str(store.root), "scrape", "--now", now.isoformat()])
    return cli.main(
        ["--root", str(store.root), "deliver", "--live", "--now", now.isoformat()]
    )


def test_a_source_outage_tells_only_the_people_who_asked(store, monkeypatch):
    sender = _Recorder()
    _outage_store(store, monkeypatch, sender)
    # 12:00 UTC = 08:00 New York, past both recipients' 07:00 delivery time.
    now = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)

    assert _fail_scrape_then_deliver(store, monkeypatch, now) == 0

    assert [telegram_id for telegram_id, _ in sender.sent] == [111]
    assert "could not check listings today" in sender.sent[0][1]
    assert "unavailable" in sender.sent[0][1]


def test_a_source_outage_notice_is_sent_once_per_day_not_once_per_tick(
    store, monkeypatch
):
    sender = _Recorder()
    _outage_store(store, monkeypatch, sender)
    now = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    _fail_scrape_then_deliver(store, monkeypatch, now)
    assert len(sender.sent) == 1

    for minutes in (10, 20, 30, 40, 50):
        cli.main(
            [
                "--root",
                str(store.root),
                "deliver",
                "--live",
                "--now",
                (now + timedelta(minutes=minutes)).isoformat(),
            ]
        )
    assert len(sender.sent) == 1, f"one outage, one message; got {sender.sent}"


def test_no_outage_notice_before_the_recipients_delivery_time(store, monkeypatch):
    sender = _Recorder()
    _outage_store(store, monkeypatch, sender)
    # 11:10 UTC = 06:10 New York: the scrape has run and failed, but nobody is
    # due until 07:00. A failure must not wake anyone early.
    now = datetime(2026, 1, 15, 11, 10, tzinfo=timezone.utc)
    _fail_scrape_then_deliver(store, monkeypatch, now)
    assert sender.sent == []


def test_no_outage_notice_before_scrape_time(store, monkeypatch):
    sender = _Recorder()
    _outage_store(store, monkeypatch, sender)
    # 10:00 UTC = 05:00 New York, before the 06:00 scrape gate: the empty cache
    # is the normal overnight state, not an outage.
    now = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(
        cli, "configured_adapters", lambda *a, **k: ([_FailingAdapter()], [])
    )
    cli.main(["--root", str(store.root), "scrape", "--now", now.isoformat()])
    cli.main(["--root", str(store.root), "deliver", "--live", "--now", now.isoformat()])
    assert sender.sent == []


def test_an_outage_notice_does_not_consume_the_day(store, monkeypatch):
    """If the source recovers later the same day, the real listings still go out."""
    sender = _Recorder()
    _outage_store(store, monkeypatch, sender)
    settings = store.load_settings()
    morning = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    _fail_scrape_then_deliver(store, monkeypatch, morning)
    assert len(sender.sent) == 1

    # The source comes back at midday and the scrape finally succeeds.
    afternoon = datetime(2026, 1, 15, 18, tzinfo=timezone.utc)
    day = scrape_day(settings, afternoon)
    store.cache_raw(
        day,
        "zillow",
        [make_listing(source="zillow", state="TX")],
        metadata={
            "query_scope": [{"city": "austin", "state": "TX"}],
            "scrape_day": day.isoformat(),
            "scrape_timezone": settings.scrape_timezone,
            "queries_planned": 1,
            "queries_completed": 1,
        },
    )
    cli.main(
        ["--root", str(store.root), "deliver", "--live", "--now", afternoon.isoformat()]
    )

    listing_messages = [text for _, text in sender.sent if "zillow.com" in text or "Main St" in text]
    assert listing_messages, f"recovery must still deliver listings; got {sender.sent}"
