"""Shared fixtures for the file-based build."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from chc_rental.models import AllowlistEntry, Profile, Search
from chc_rental.store import Store

# 09:00 America/New_York in January is 14:00 UTC — after the default delivery time.
DUE_NOW = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path)
    store.initialize()
    return store


def make_search(**overrides) -> Search:
    defaults: dict[str, Any] = dict(
        name="Downtown",
        city="Austin",
        district=None,
        price_min=1000,
        price_max=3000,
        property_types=["apartment"],
        bed_min=1,
        bed_max=3,
        bath_min=1.0,
        bath_max=3.0,
        required_features=[],
        excluded_features=[],
        daily_cap=5,
    )
    defaults.update(overrides)
    return Search(**defaults)


def make_person(telegram_id: int = 111, **overrides) -> AllowlistEntry:
    profile = overrides.pop("profile", None) or Profile(searches=[make_search()])
    defaults: dict[str, Any] = dict(
        telegram_id=telegram_id,
        display_name=f"Person {telegram_id}",
        active=True,
        profile=profile,
    )
    defaults.update(overrides)
    return AllowlistEntry(**defaults)


def make_listing(**overrides) -> dict[str, Any]:
    defaults: dict[str, Any] = dict(
        source="feed",
        source_listing_id="L1",
        url="https://example.com/listing/1",
        address="100 Main St",
        unit=None,
        city="Austin",
        district=None,
        price=2000,
        property_type="apartment",
        beds=2,
        baths=2.0,
        features=[],
    )
    defaults.update(overrides)
    return defaults
