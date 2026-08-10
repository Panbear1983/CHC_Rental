"""Offline orchestration dry-run tests: fixture input, local SQLite, no writes."""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

from chc_rental.db import Database
from chc_rental.models import PreferenceProfileCreate
from chc_rental.repositories import AllowlistRepository, AuditRepository, ProfileRepository


def make_profile(**overrides):
    defaults = dict(
        profile_name="Downtown",
        city="Austin",
        district=None,
        price_min=1000,
        price_max=2000,
        property_types=["apartment"],
        bed_min=1,
        bed_max=2,
        bath_min=1.0,
        bath_max=2.0,
        required_features=[],
        excluded_features=[],
        daily_cap=2,
        delivery_time="09:00",
        timezone="America/New_York",
    )
    defaults.update(overrides)
    return PreferenceProfileCreate(**defaults)


def fixture_listing(source_listing_id="a"):
    return {
        "source": "fixture",
        "source_listing_id": source_listing_id,
        "address": f"{source_listing_id} Main St",
        "city": "Austin",
        "price": 1500,
        "property_type": "apartment",
        "beds": 1,
        "baths": 1.0,
        "features": [],
    }


def create_db(path):
    db = Database(path)
    allowlist = AllowlistRepository(db)
    allowlist.add_user(987654321, "Fixture User")
    profile = ProfileRepository(db, allowlist, AuditRepository(db)).create_profile(987654321, make_profile())
    db.close()
    return profile


def test_due_profile_produces_preview_without_mutating_ledger_or_budget(tmp_path):
    from chc_rental.dry_run import run_dry_run

    db_path = tmp_path / "rental.sqlite3"
    profile = create_db(db_path)
    result = run_dry_run(
        db_path,
        [fixture_listing()],
        now_utc=datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc),
        global_daily_budget=3,
    )

    assert result.summary == {
        "active_profiles": 1,
        "allocated_listings": 1,
        "due_profiles": 1,
        "eligible_candidates": 1,
        "fixture_listings": 1,
        "unique_listings": 1,
    }
    assert result.preview == (
        "DRY-RUN NOTIFICATION PREVIEW\n"
        f"profile={profile.id} name=Downtown listings=1\n"
        "- id:fixture:a | a Main St | Austin | $1500\n"
        'SUMMARY {"active_profiles": 1, "allocated_listings": 1, "due_profiles": 1, '
        '"eligible_candidates": 1, "fixture_listings": 1, "unique_listings": 1}'
    )
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM delivery_ledger").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM daily_budget_ledger").fetchone()[0] == 0
    finally:
        connection.close()


def test_not_due_profile_is_excluded_from_preview(tmp_path):
    from chc_rental.dry_run import run_dry_run

    db_path = tmp_path / "rental.sqlite3"
    create_db(db_path)
    result = run_dry_run(
        db_path,
        [fixture_listing()],
        now_utc=datetime(2026, 1, 15, 13, 59, tzinfo=timezone.utc),
        global_daily_budget=3,
    )

    assert result.summary == {
        "active_profiles": 1,
        "allocated_listings": 0,
        "due_profiles": 0,
        "eligible_candidates": 0,
        "fixture_listings": 1,
        "unique_listings": 1,
    }
    assert "profile=" not in result.preview


def test_already_sent_ledger_listing_is_not_eligible_but_does_not_mutate(tmp_path):
    from chc_rental.dry_run import run_dry_run

    db_path = tmp_path / "rental.sqlite3"
    profile = create_db(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO delivery_ledger (telegram_user_id, profile_id, listing_key, status, updated_at) "
            "VALUES (?, ?, ?, 'sent', '2026-01-14T14:00:00Z')",
            (987654321, profile.id, "id:fixture:a"),
        )
        connection.commit()
    finally:
        connection.close()

    result = run_dry_run(
        db_path,
        [fixture_listing()],
        now_utc=datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc),
        global_daily_budget=3,
    )

    assert result.summary["due_profiles"] == 1
    assert result.summary["eligible_candidates"] == 0
    assert result.summary["allocated_listings"] == 0
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT status FROM delivery_ledger").fetchone()[0] == "sent"
    finally:
        connection.close()


def test_matching_candidates_are_fairly_allocated_between_due_profiles(tmp_path):
    from chc_rental.dry_run import run_dry_run

    db_path = tmp_path / "rental.sqlite3"
    first = create_db(db_path)
    db = Database(db_path)
    try:
        allowlist = AllowlistRepository(db)
        allowlist.add_user(111222333, "Second Fixture User")
        second = ProfileRepository(db, allowlist, AuditRepository(db)).create_profile(
            111222333,
            make_profile(profile_name="Other", city="Dallas"),
        )
    finally:
        db.close()

    result = run_dry_run(
        db_path,
        [fixture_listing("a"), {**fixture_listing("b"), "city": "Dallas"}],
        now_utc=datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc),
        global_daily_budget=2,
    )

    assert result.summary["eligible_candidates"] == 2
    assert result.summary["allocated_listings"] == 2
    assert result.preview.splitlines()[1:5] == [
        f"profile={first.id} name=Downtown listings=1",
        "- id:fixture:a | a Main St | Austin | $1500",
        f"profile={second.id} name=Other listings=1",
        "- id:fixture:b | b Main St | Dallas | $1500",
    ]


def test_due_profile_with_no_matches_can_preview_no_results_notification(tmp_path):
    from chc_rental.dry_run import run_dry_run

    db_path = tmp_path / "rental.sqlite3"
    create_db(db_path)
    db = Database(db_path)
    try:
        profile = ProfileRepository(db, AllowlistRepository(db), AuditRepository(db)).list_profiles(987654321)[0]
        ProfileRepository(db, AllowlistRepository(db), AuditRepository(db)).update_profile(
            987654321, profile.id, __import__("chc_rental.models", fromlist=["PreferenceProfileUpdate"]).PreferenceProfileUpdate(
                notify_on_no_results=True
            )
        )
    finally:
        db.close()

    result = run_dry_run(
        db_path, [], now_utc=datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc), global_daily_budget=3
    )

    assert "listings=0 no-results-notification=yes" in result.preview
    assert result.summary["no_results_notifications"] == 1


def test_malformed_fixture_is_rejected(tmp_path):
    import pytest

    from chc_rental.dry_run import load_fixture_listings

    fixture_path = tmp_path / "bad.json"
    fixture_path.write_text('{"listings": [{"source": "fixture"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid fixture listing"):
        load_fixture_listings(fixture_path)


def test_module_cli_prints_preview_without_recipient_identifier(tmp_path):
    db_path = tmp_path / "rental.sqlite3"
    create_db(db_path)
    fixture_path = tmp_path / "listings.json"
    fixture_path.write_text(json.dumps([fixture_listing()]), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "chc_rental.dry_run",
            "--db",
            str(db_path),
            "--fixture",
            str(fixture_path),
            "--now",
            "2026-01-15T14:00:00Z",
            "--global-daily-budget",
            "3",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("DRY-RUN NOTIFICATION PREVIEW\n")
    assert "SUMMARY " in completed.stdout
    assert "987654321" not in completed.stdout
