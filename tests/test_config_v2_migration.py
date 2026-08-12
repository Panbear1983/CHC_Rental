"""Explicit schema-v2 migration keeps legacy daily config safe and stable."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from chc_rental.models import Allowlist, DeliveryMode, Profile
from chc_rental.store import Store, StoreError
from chc_rental.tui.controller import TuiController

from tests.conftest import make_person, make_search


def _seed_v1(store: Store) -> None:
    store.save_allowlist(
        Allowlist(
            schema_version=1,
            people=[
                make_person(
                    111,
                    profile=Profile(
                        searches=[
                            make_search(name="First", state="TX"),
                            make_search(name="Second", state="TX"),
                        ]
                    ),
                )
            ],
        )
    )


def test_v1_config_is_readable_without_creating_the_alert_database(store):
    _seed_v1(store)
    assert len(store.load_allowlist().people[0].profile.searches) == 2
    assert store.alert_db_path.exists() is False
    status = store.config_v2_status()
    assert status["allowlist_version"] == 1
    assert status["missing_search_ids"] == 2
    assert status["ready"] is False
    assert store.alert_db_path.exists() is False


def test_migration_assigns_search_ids_once_and_persists_new_profile_defaults(store):
    _seed_v1(store)
    status = store.migrate_config_v2()
    assert status["ready"] is True
    migrated = store.load_allowlist()
    assert migrated.schema_version == 2
    ids = [search.search_id for search in migrated.people[0].profile.searches]
    assert None not in ids and len(set(ids)) == 2
    assert migrated.people[0].profile.delivery_mode is DeliveryMode.DAILY
    assert migrated.people[0].profile.quiet_hours_start is None

    store.migrate_config_v2()
    assert [
        search.search_id for search in store.load_allowlist().people[0].profile.searches
    ] == ids
    assert store.load_settings().schema_version == 2


def test_renaming_a_migrated_search_preserves_its_stable_id(store):
    _seed_v1(store)
    store.migrate_config_v2()
    controller = TuiController(store)
    original = controller.list_searches(111)[0]
    form = original.model_dump(mode="json", exclude={"search_id"})
    form["name"] = "Renamed"
    # The form parser expects comma-oriented text for these fields.
    form["property_types"] = ", ".join(form["property_types"])
    form["required_features"] = ""
    form["excluded_features"] = ""
    controller.update_search(111, 0, form)
    renamed = controller.list_searches(111)[0]
    assert renamed.name == "Renamed"
    assert renamed.search_id == original.search_id


def test_schema_v2_rejects_missing_or_duplicate_search_ids():
    person = make_person(
        111,
        profile=Profile(searches=[make_search(name="A"), make_search(name="B")]),
    )
    with pytest.raises(ValueError, match="stable search_id"):
        Allowlist(schema_version=2, people=[person])

    duplicate = "11111111-1111-4111-8111-111111111111"
    person.profile.searches[0].search_id = duplicate
    person.profile.searches[1].search_id = duplicate
    with pytest.raises(ValueError, match="search_id must be unique"):
        Allowlist(schema_version=2, people=[person])


def test_duplicate_existing_id_aborts_without_rewriting_config(store):
    duplicate = "11111111-1111-4111-8111-111111111111"
    raw = yaml.safe_load(store.allowlist_path.read_text(encoding="utf-8"))
    raw["people"] = [
        {
            "telegram_id": 111,
            "display_name": "Person",
            "active": True,
            "profile": {
                "searches": [
                    make_search(name="A", search_id=duplicate).model_dump(mode="json"),
                    make_search(name="B", search_id=duplicate).model_dump(mode="json"),
                ]
            },
        }
    ]
    store.allowlist_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    before = store.allowlist_path.read_bytes()
    with pytest.raises(StoreError, match="duplicate search_id"):
        store.migrate_config_v2()
    assert store.allowlist_path.read_bytes() == before


def test_write_failure_restores_both_yaml_files(store, monkeypatch):
    _seed_v1(store)
    allowlist_before = store.allowlist_path.read_bytes()
    settings_before = store.settings_path.read_bytes()
    real_write = store._write_yaml

    def fail_on_settings(path: Path, payload):
        if path == store.settings_path:
            raise OSError("simulated disk failure")
        return real_write(path, payload)

    monkeypatch.setattr(store, "_write_yaml", fail_on_settings)
    with pytest.raises(OSError, match="simulated disk failure"):
        store.migrate_config_v2()
    assert store.allowlist_path.read_bytes() == allowlist_before
    assert store.settings_path.read_bytes() == settings_before


def test_quiet_hours_require_a_valid_pair():
    with pytest.raises(ValueError, match="set together"):
        Profile(quiet_hours_start="22:00")
    with pytest.raises(ValueError, match="entire day"):
        Profile(quiet_hours_start="22:00", quiet_hours_end="22:00")
    profile = Profile(quiet_hours_start="22:00", quiet_hours_end="08:00")
    assert profile.quiet_hours_start == "22:00" and profile.quiet_hours_end == "08:00"
