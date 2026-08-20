"""CLI operator-alert behavior that spans fetch and delivery summaries."""

from datetime import datetime, timezone
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
        {"fetch": truncated_fetch(), "summary": result.summary},
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
