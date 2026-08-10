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


def test_run_log_round_trip(store):
    store.write_run_log(DAY, {"summary": {"planned_pushes": 3}})
    assert store.read_run_log(DAY)["summary"]["planned_pushes"] == 3
    assert store.latest_run_log()["summary"]["planned_pushes"] == 3


def test_cache_round_trip(store):
    store.cache_raw(DAY, "feed", [{"a": 1}])
    assert store.load_cached(DAY, "feed") == [{"a": 1}]
    assert store.load_cached(DAY, "other") is None


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
