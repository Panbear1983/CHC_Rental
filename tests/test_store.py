"""The store is the single gate, so its safety properties are tested directly.

`budgets.py` and `delivery.py` in the SQLite build had zero test coverage, and
that is exactly where the two worst defects lived. The quota reservation and the
seen ledger are their replacements, so they get tests in the same change that
introduces them.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from chc_rental.models import Allowlist, Settings
from chc_rental.store import Store, StoreError

from tests.conftest import make_person

DAY = date(2026, 1, 15)


def _reserve_once(args: tuple[str, int]) -> bool:
    """Module-level so it survives pickling to a spawned process."""
    root, limit = args
    return Store(root).reserve_request(DAY, "feed", per_source_limit=limit, global_limit=limit)


def test_initialize_creates_config_with_defaults(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    assert store.allowlist_path.exists()
    assert store.settings_path.exists()
    assert store.load_allowlist().people == []
    assert store.load_settings().global_daily_request_budget == 100
    assert store.load_settings().source_request_budget("rentcast") == 50
    assert store.load_settings().source_request_budget("zillow") == 5
    assert store.load_settings().zillow_enabled is False


def test_legacy_owner_alert_key_loads_and_saves_under_operator_name(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    store.settings_path.write_text("owner_telegram_id: 424242\n", encoding="utf-8")
    loaded = store.load_settings()
    assert loaded.operator_alert_telegram_id == 424242
    store.save_settings(loaded)
    saved = store.settings_path.read_text(encoding="utf-8")
    assert "operator_alert_telegram_id: 424242" in saved
    assert "owner_telegram_id" not in saved


def test_whole_run_lock_refuses_an_overlapping_cycle(store):
    other = Store(store.root)
    with store.try_run_lock() as first:
        assert first is True
        with other.try_run_lock() as second:
            assert second is False
    with other.try_run_lock() as after_release:
        assert after_release is True


def test_allowlist_round_trip_preserves_every_field(store):
    original = Allowlist(people=[make_person(111), make_person(222)])
    store.save_allowlist(original)
    assert store.load_allowlist().model_dump() == original.model_dump()


def test_saving_config_leaves_a_backup(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    store.save_allowlist(Allowlist(people=[make_person(222)]))
    backups = list(store.backup_dir.glob("allowlist-*.yaml"))
    assert len(backups) == 1, "the second save should back up the first"


def test_config_files_are_written_owner_only(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    assert oct(store.allowlist_path.stat().st_mode)[-3:] == "600"


def test_invalid_yaml_raises_a_clear_store_error(store):
    store.allowlist_path.write_text("this: is: not: valid: yaml:\n", encoding="utf-8")
    with pytest.raises(StoreError):
        store.load_allowlist()


def test_config_failing_validation_names_the_file(store):
    store.allowlist_path.write_text("people:\n  - telegram_id: -5\n", encoding="utf-8")
    with pytest.raises(StoreError) as excinfo:
        store.load_allowlist()
    assert "allowlist.yaml" in str(excinfo.value)


def test_edit_allowlist_writes_changes_on_exit(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    with store.edit_allowlist() as allowlist:
        allowlist.people.append(make_person(222))
    assert {p.telegram_id for p in store.load_allowlist().people} == {111, 222}


def test_edit_allowlist_rejects_an_invalid_result(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    with pytest.raises(Exception):
        with store.edit_allowlist() as allowlist:
            allowlist.people.append(make_person(111))  # duplicate id
    assert len(store.load_allowlist().people) == 1, "the bad edit must not land"


def test_atomic_write_leaves_no_temp_files_behind(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    assert list(store.config_dir.glob(".tmp-*")) == []


# --- seen ledger ------------------------------------------------------------


def test_seen_ledger_is_empty_for_an_unknown_person(store):
    assert store.seen_keys(999) == set()
    assert store.last_sent_at(999) is None


def test_mark_seen_then_has_seen(store):
    store.mark_seen(111, "k1", search_name="Downtown", url="https://example.com/1")
    assert store.has_seen(111, "k1") is True
    assert store.has_seen(111, "k2") is False
    assert store.has_seen(222, "k1") is False, "ledgers are per person"


def test_last_sent_at_returns_the_most_recent_entry(store):
    store.mark_seen(111, "k1", search_name="s", url="https://example.com/1")
    store.mark_seen(111, "k2", search_name="s", url="https://example.com/2")
    stamp = store.last_sent_at(111)
    assert stamp is not None and stamp.tzinfo is not None


def test_test_delivery_suppresses_only_its_recipient_without_advancing_due_gate(store):
    stamp = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    store.mark_seen(
        111,
        "v3:ny:brooklyn::1%20main%20st:",
        search_name="Brooklyn",
        url="https://www.zillow.com/1_zpid/",
        now_utc=stamp,
        channel="test",
        telegram_message_id="700",
        chat_id="111",
    )

    assert store.active_seen_keys(111, now_utc=stamp) == {
        "v3:ny:brooklyn::1%20main%20st:"
    }
    assert store.active_seen_keys(222, now_utc=stamp) == set()
    assert store.last_sent_at(111) is None


def test_repeat_delivery_is_audited_without_restarting_original_retention(store):
    original = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    repeated = original + timedelta(days=80)
    after_window = original + timedelta(days=91)
    key = "v3:ny:brooklyn::1%20main%20st:"
    store.mark_seen(
        111,
        key,
        search_name="Brooklyn",
        url="https://www.zillow.com/1_zpid/",
        now_utc=original,
        channel="test",
    )
    store.mark_seen(
        111,
        key,
        search_name="Brooklyn",
        url="https://www.zillow.com/1_zpid/",
        now_utc=repeated,
        channel="test",
        repeat_override=True,
    )

    records = [
        json.loads(line)
        for line in store.seen_path(111).read_text(encoding="utf-8").splitlines()
    ]
    assert [record["repeat_override"] for record in records] == [False, True]
    assert store.active_seen_keys(111, now_utc=repeated) == {key}
    assert store.active_seen_keys(111, now_utc=after_window) == set()
    assert store.last_sent_at(111) is None


def test_a_corrupt_ledger_line_does_not_hide_the_rest(store):
    store.mark_seen(111, "k1", search_name="s", url="https://example.com/1")
    with store.seen_path(111).open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    store.mark_seen(111, "k2", search_name="s", url="https://example.com/2")
    assert store.seen_keys(111) == {"k1", "k2"}


# --- quota ------------------------------------------------------------------


def test_reserve_request_counts_up_to_the_per_source_limit(store):
    granted = [
        store.reserve_request(DAY, "feed", per_source_limit=3, global_limit=100) for _ in range(5)
    ]
    assert granted == [True, True, True, False, False]
    assert store.quota_used(DAY, "feed") == 3


def test_reserve_request_enforces_the_global_ceiling_across_sources(store):
    assert store.reserve_request(DAY, "a", per_source_limit=10, global_limit=2) is True
    assert store.reserve_request(DAY, "b", per_source_limit=10, global_limit=2) is True
    assert store.reserve_request(DAY, "c", per_source_limit=10, global_limit=2) is False
    assert store.quota_total(DAY) == 2


def test_quota_is_per_day(store):
    store.reserve_request(DAY, "feed", per_source_limit=1, global_limit=1)
    assert store.reserve_request(DAY, "feed", per_source_limit=1, global_limit=1) is False
    tomorrow = DAY + timedelta(days=1)
    assert store.reserve_request(tomorrow, "feed", per_source_limit=1, global_limit=1) is True


def test_concurrent_reservations_never_exceed_the_ceiling(tmp_path):
    """The old SQLite budget lost 15 of 16 concurrent updates. This must not."""
    import multiprocessing

    limit = 8
    store = Store(tmp_path)
    store.initialize()

    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=8) as pool:
        results = pool.map(_reserve_once, [(str(tmp_path), limit)] * 24)

    granted = sum(1 for value in results if value)
    assert granted == limit, f"granted {granted}, expected exactly {limit}"
    assert store.quota_used(DAY, "feed") == limit


# --- rejects, runs, retention ----------------------------------------------


def test_rejected_records_are_retained_and_counted(store):
    store.record_rejected(DAY, source="feed", reason="bad price", raw={"price": -5})
    store.record_rejected(DAY, source="feed", reason="bad url", raw={"url": "ftp://x"})
    assert store.rejected_count(DAY) == 2
    body = store.rejected_path(DAY).read_text(encoding="utf-8")
    assert "bad price" in body and "bad url" in body


def test_identical_rejected_record_is_logged_only_once_per_day(store):
    raw = {"source_listing_id": "bad-1", "price": None}
    store.record_rejected(DAY, source="feed", reason="bad price", raw=raw)
    store.record_rejected(DAY, source="feed", reason="bad price", raw=raw)
    assert store.rejected_count(DAY) == 1


def test_rejected_dedup_recognizes_legacy_lines_without_a_fingerprint(store):
    raw = {"source_listing_id": "bad-legacy", "price": None}
    legacy = {"at": "2026-01-15T00:00:00+00:00", "source": "feed",
              "reason": "bad price", "raw": raw}
    store.rejected_path(DAY).write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    store.record_rejected(DAY, source="feed", reason="bad price", raw=raw)
    assert store.rejected_count(DAY) == 1


def test_run_log_round_trip(store):
    store.write_run_log(DAY, {"summary": {"planned_pushes": 3}})
    assert store.read_run_log(DAY)["summary"]["planned_pushes"] == 3
    assert store.latest_run_log()["summary"]["planned_pushes"] == 3


def test_cache_round_trip(store):
    store.cache_raw(DAY, "feed", [{"a": 1}])
    assert store.load_cached(DAY, "feed") == [{"a": 1}]
    assert store.load_cached(DAY, "other") is None


def test_scoped_cache_requires_matching_query_metadata(store):
    scope = [{"city": "austin", "state": "TX"}]
    store.cache_raw(DAY, "feed", [{"a": 1}], metadata={"query_scope": scope})
    assert store.load_cached(DAY, "feed", expected_query_scope=scope) == [{"a": 1}]
    assert store.load_cached(
        DAY, "feed", expected_query_scope=[{"city": "dallas", "state": "TX"}]
    ) is None


def test_legacy_cache_without_scope_is_not_reused_by_query_aware_fetch(store):
    store.cache_raw(DAY, "feed", [{"stale": True}])
    scope = [{"city": "austin", "state": "TX"}]
    assert store.load_cached(DAY, "feed", expected_query_scope=scope) is None


def test_prune_removes_only_expired_state(store):
    settings = Settings(cache_retention_days=7, rejected_retention_days=7, backup_retention_days=7)
    old = date(2026, 1, 1)
    recent = date(2026, 1, 14)
    for day in (old, recent):
        store.write_run_log(day, {"summary": {}})
        store.record_rejected(day, source="feed", reason="x", raw={})
    removed = store.prune(today=date(2026, 1, 15), settings=settings)
    assert removed["runs"] == 1 and removed["rejected"] == 1
    assert store.read_run_log(recent) is not None
    assert store.read_run_log(old) is None


# --- per-run logs and pruning (2026-08-11 hardening) -------------------------


def test_record_run_keeps_history_and_latest_wins(store):
    from datetime import datetime, timezone

    noon = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 1, 15, 19, 0, tzinfo=timezone.utc)
    store.record_run(noon, {"summary": {"planned_pushes": 0}})
    store.record_run(evening, {"summary": {"planned_pushes": 5}})
    assert store.latest_run_log()["summary"]["planned_pushes"] == 5
    runs = list((store.state_dir / "runs").glob("*.json"))
    assert len(runs) == 2, "each run must keep its own file"


def test_latest_run_log_prefers_a_per_run_file_over_the_same_days_daily_file(store):
    from datetime import date, datetime, timezone

    store.write_run_log(date(2026, 1, 15), {"summary": {"planned_pushes": 1}})
    store.record_run(
        datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc), {"summary": {"planned_pushes": 9}}
    )
    assert store.latest_run_log()["summary"]["planned_pushes"] == 9


def test_prune_removes_expired_per_run_files(store):
    from datetime import date, datetime, timezone

    from chc_rental.models import Settings

    old = datetime(2025, 12, 1, 12, 0, tzinfo=timezone.utc)
    fresh = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    store.record_run(old, {"summary": {}})
    store.record_run(fresh, {"summary": {}})
    store.prune(today=date(2026, 1, 15), settings=Settings())
    remaining = [p.name for p in (store.state_dir / "runs").glob("*.json")]
    assert remaining == ["2026-01-15T120000Z.json"]


def test_prune_drops_only_expired_seen_records(store):
    import json as jsonlib
    from datetime import date

    from chc_rental.models import Settings

    store.mark_seen(111, "old-key", search_name="s", url="https://example.com/1")
    store.mark_seen(111, "fresh-key", search_name="s", url="https://example.com/2")
    lines = store.seen_path(111).read_text(encoding="utf-8").splitlines()
    doctored = []
    for line in lines:
        record = jsonlib.loads(line)
        if record["key"] == "old-key":
            record["sent_at"] = "2025-01-01T00:00:00+00:00"
        doctored.append(jsonlib.dumps(record))
    store.seen_path(111).write_text("".join(f"{l}\n" for l in doctored), encoding="utf-8")

    removed = store.prune(today=date(2026, 1, 15), settings=Settings(seen_retention_days=90))
    assert removed["seen"] == 1
    assert store.seen_keys(111) == {"fresh-key"}


def test_seen_retention_zero_means_keep_forever(store):
    from datetime import date

    from chc_rental.models import Settings

    store.mark_seen(111, "k", search_name="s", url="https://example.com/1")
    removed = store.prune(today=date(2026, 1, 15), settings=Settings(seen_retention_days=0))
    assert removed["seen"] == 0
    assert store.seen_keys(111) == {"k"}


def test_mark_notified_advances_last_sent_at_without_touching_listing_keys(store):
    assert store.last_sent_at(111) is None
    store.mark_notified(111)
    assert store.last_sent_at(111) is not None
    assert all(k.startswith("notice:") for k in store.seen_keys(111))
