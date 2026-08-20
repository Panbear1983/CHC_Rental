"""Unattended tick, disabled LaunchAgent, CLI backup, and missing-token recovery."""

from __future__ import annotations

import json
import plistlib
from datetime import timedelta
from pathlib import Path

import chc_rental.cli as cli
from chc_rental.models import Allowlist, Profile
from chc_rental.scheduler import IncrementalScheduler
from chc_rental.store import Store

from tests.conftest import make_person, make_search
from tests.test_listing_events import NOW, raw_listing


def configured_store(tmp_path) -> Store:
    store = Store(tmp_path)
    store.initialize()
    store.save_allowlist(
        Allowlist(
            people=[
                make_person(
                    111, profile=Profile(searches=[make_search(name="Primary", state="TX")])
                )
            ]
        )
    )
    store.migrate_config_v2()
    store.migrate_alert_ledger()
    return store


def test_incremental_launchagent_is_separate_and_disabled_by_default():
    root = Path(__file__).parents[1]
    with (root / "scripts/com.chcrental.alerts.plist").open("rb") as handle:
        alerts = plistlib.load(handle)
    with (root / "scripts/com.chcrental.daily.plist").open("rb") as handle:
        daily = plistlib.load(handle)
    assert alerts["Label"] == "com.chcrental.alerts"
    assert alerts["Disabled"] is True and alerts["RunAtLoad"] is False
    assert alerts["StartInterval"] == 900
    assert "alerts tick --live" in alerts["ProgramArguments"][-1]
    assert daily["Label"] == "com.chcrental.daily"
    assert "StartInterval" not in daily
    assert [
        item["Minute"] for item in daily["StartCalendarInterval"]
    ] == [0, 10, 20, 30, 40, 50]
    assert "./dashboard.sh scrape " in daily["ProgramArguments"][-1]
    assert "./dashboard.sh deliver --live " in daily["ProgramArguments"][-1]
    assert " run --live " not in daily["ProgramArguments"][-1]


def test_cli_tick_without_live_or_fixture_is_read_only(tmp_path, capsys):
    store = configured_store(tmp_path)
    before = store.alert_db_path.read_bytes()
    assert cli.main(["--root", str(tmp_path), "alerts", "tick"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert "no source" in payload["note"]
    assert store.alert_db_path.read_bytes() == before
    assert list(store.backup_dir.glob("alerts-daily-*.sqlite3")) == []


def test_offline_fixture_tick_processes_and_backs_up_without_telegram(
    tmp_path, capsys, monkeypatch
):
    store = configured_store(tmp_path)
    fixture = tmp_path / "actor-items.json"
    fixture.write_text(json.dumps([raw_listing("fixture-a")]))
    monkeypatch.setattr(
        cli, "build_sender", lambda path: (_ for _ in ()).throw(AssertionError("no Telegram"))
    )
    code = cli.main(
        [
            "--root",
            str(tmp_path),
            "alerts",
            "tick",
            "--fixture",
            str(fixture),
            "--now",
            NOW.isoformat(),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"]["records"] == 1
    assert payload["delivery"] is None
    assert Path(payload["backup"]).exists()
    assert store.quota_used(NOW.date(), "zillow") == 0
    assert payload["tick_id"] is not None
    with store.event_store().connection() as connection:
        tick = connection.execute(
            "SELECT mode, status FROM scheduler_ticks WHERE tick_id=?",
            (payload["tick_id"],),
        ).fetchone()
    assert tuple(tick) == ("fixture", "ok")


def test_offline_fixture_tick_disables_owner_alert_transport(
    tmp_path, capsys, monkeypatch
):
    store = configured_store(tmp_path)
    store.event_store().record_source_failure(
        "future-source",
        now_utc=NOW,
        error_class="fixture-test",
        error_message="must stay local",
        threshold=1,
        cooldown_minutes=30,
        immediate_open=True,
    )
    fixture = tmp_path / "actor-items.json"
    fixture.write_text("[]")
    monkeypatch.setattr(
        cli,
        "_alert_operator",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("fixture tick must not call operator Telegram")
        ),
    )
    assert cli.main(
        [
            "--root",
            str(tmp_path),
            "alerts",
            "tick",
            "--fixture",
            str(fixture),
            "--now",
            NOW.isoformat(),
        ]
    ) == 0
    capsys.readouterr()
    assert store.event_store().pending_source_alerts()[0].source == "future-source"


def test_missing_apify_token_opens_breaker_without_source_or_telegram_call(tmp_path):
    store = configured_store(tmp_path)
    settings = store.load_settings().model_copy(
        update={"incremental_alerts_enabled": True, "zillow_enabled": True}
    )
    store.save_settings(settings)
    report = IncrementalScheduler(
        store,
        collector=None,
        sender=None,
        source_unavailable_reason="APIFY_TOKEN missing in test",
    ).tick(now_utc=NOW)
    assert any("APIFY_TOKEN" in item for item in report.warnings)
    breaker = store.event_store().source_breakers()[0]
    assert breaker.state == "open" and breaker.last_error_class == "missing_credential"


def test_cli_backup_runs_verified_disposable_restore_drill(tmp_path, capsys):
    configured_store(tmp_path)
    assert (
        cli.main(
            [
                "--root",
                str(tmp_path),
                "alerts",
                "backup",
                "--now",
                NOW.isoformat(),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["verification"]["integrity"] == "ok"
    assert payload["restore_drill"]["live_database_untouched"] is True
    assert Path(payload["backup"]).is_file()
