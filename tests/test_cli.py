"""CLI operator-alert behavior that spans fetch and delivery summaries."""

from datetime import datetime, timezone
import json

import chc_rental.cli as cli
from chc_rental.models import Allowlist, Profile
from chc_rental.pipeline import PipelineResult
from chc_rental.store import Store

from tests.conftest import make_person, make_search


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
    monkeypatch.setattr(cli, "_alert_owner", lambda store, env_file, text: sent.append(text))
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


def test_alert_status_is_read_only_and_reports_pending_migrations(tmp_path, capsys):
    assert cli.main(["--root", str(tmp_path), "alerts", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["ready"] is False
    assert payload["ledger"]["pending_versions"] == [1, 2, 3]
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
