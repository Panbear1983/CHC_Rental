"""Fetch layer: planner dedup, RentCast mapping, budget/cache/retry orchestration.

No test performs network I/O: the adapter's HTTP method is exercised through a
fake `urlopen`, and `fetch_daily` through fake adapters.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chc_rental.fetch import (
    FetchReport,
    PoolFetchReport,
    fetch_daily,
    fetch_many_daily,
    is_scrape_time,
)
from chc_rental.models import Allowlist, Listing, Profile, Settings
from chc_rental.sources.base import (
    SourceAuthError,
    SourceQuery,
    SourceRateLimitError,
    SourceUnavailableError,
)
from chc_rental.sources import configured_adapters
from chc_rental.sources.planner import plan_incremental_queries, plan_queries
from chc_rental.sources.rentcast import RentCastAdapter, _parse_retry_after
from chc_rental.sources.zillow import (
    ZillowRentalAdapter,
    rental_search_url,
    resolve_map_bounds,
)

from tests.conftest import make_person, make_search

# 12:00 UTC = 08:00 America/New_York in January — exactly the default scrape time.
FETCH_NOW = datetime(2026, 1, 15, 13, 5, tzinfo=timezone.utc)
BEFORE_SCRAPE = datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)  # 06:00 New York

RAW_RENTCAST = {
    "id": "3821-Hargis-St,-Austin,-TX-78723",
    "formattedAddress": "3821 Hargis St, Austin, TX 78723",
    "addressLine1": "3821 Hargis St",
    "addressLine2": "Apt 12",
    "city": "Austin",
    "state": "TX",
    "zipCode": "78723",
    "county": "Travis",
    "propertyType": "Single Family",
    "bedrooms": 3,
    "bathrooms": 2,
    "squareFootage": 1428,
    "status": "Active",
    "price": 2100,
    "listedDate": "2026-01-10T00:00:00.000Z",
}

RAW_ZILLOW_RENTAL = {
    "zpid": "123456",
    "detailUrl": "/homedetails/100-Main-St-APT-4B-Austin-TX/123456_zpid/",
    "statusType": "FOR_RENT",
    "addressStreet": "100 Main St Apt 4B",
    "addressCity": "Austin",
    "addressState": "TX",
    "addressZipcode": "78701",
    "unformattedPrice": 2200,
    "beds": 2,
    "baths": 1.5,
    "area": 900,
    "homeType": "APARTMENT",
}

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sources"


def source_fixture(source: str, name: str) -> dict:
    return json.loads((FIXTURE_ROOT / source / name).read_text(encoding="utf-8"))


# --- planner ----------------------------------------------------------------


def test_two_people_watching_one_city_produce_one_query():
    a = make_person(111, profile=Profile(searches=[make_search(state="TX")]))
    b = make_person(222, profile=Profile(searches=[make_search(name="Other", state="tx")]))
    queries, warnings = plan_queries(Allowlist(people=[a, b]))
    assert queries == [SourceQuery(city="Austin", state="TX")]
    assert warnings == []


def test_inactive_people_and_searches_are_not_fetched_for():
    inactive_person = make_person(
        111, active=False, profile=Profile(searches=[make_search(state="TX")])
    )
    inactive_search = make_person(
        222, profile=Profile(searches=[make_search(state="TX", active=False)])
    )
    queries, _ = plan_queries(Allowlist(people=[inactive_person, inactive_search]))
    assert queries == []


def test_a_search_without_a_state_is_skipped_with_a_warning():
    person = make_person(111, profile=Profile(searches=[make_search()]))
    queries, warnings = plan_queries(Allowlist(people=[person]))
    assert queries == []
    assert len(warnings) == 1 and "no state" in warnings[0]


def test_identical_incremental_filter_envelopes_are_shared_across_people():
    search_a = make_search(
        name="A", state="TX", search_id="11111111-1111-4111-8111-111111111111"
    )
    search_b = make_search(
        name="B", state="TX", search_id="22222222-2222-4222-8222-222222222222"
    )
    allowlist = Allowlist(
        people=[
            make_person(111, profile=Profile(searches=[search_a])),
            make_person(222, profile=Profile(searches=[search_b])),
        ]
    )
    planned, warnings = plan_incremental_queries(allowlist)
    assert warnings == [] and len(planned) == 1
    assert {watch.telegram_id for watch in planned[0].watches} == {111, 222}


def test_incompatible_incremental_source_filters_remain_separate():
    allowlist = Allowlist(
        people=[
            make_person(
                111,
                profile=Profile(
                    searches=[
                        make_search(
                            name="Cheap",
                            state="TX",
                            price_max=1800,
                            search_id="11111111-1111-4111-8111-111111111111",
                        ),
                        make_search(
                            name="Larger",
                            state="TX",
                            bed_min=3,
                            search_id="22222222-2222-4222-8222-222222222222",
                        ),
                    ]
                ),
            )
        ]
    )
    planned, _ = plan_incremental_queries(allowlist)
    assert len(planned) == 2
    assert len({item.query_id for item in planned}) == 2


def test_local_only_feature_difference_does_not_duplicate_actor_scope():
    allowlist = Allowlist(
        people=[
            make_person(
                111,
                profile=Profile(
                    searches=[
                        make_search(
                            name="Elevator",
                            state="TX",
                            required_features=["elevator"],
                            search_id="11111111-1111-4111-8111-111111111111",
                        ),
                        make_search(
                            name="Laundry",
                            state="TX",
                            required_features=["laundry"],
                            search_id="22222222-2222-4222-8222-222222222222",
                        ),
                    ]
                ),
            )
        ]
    )
    planned, _ = plan_incremental_queries(allowlist)
    assert len(planned) == 1


# --- RentCast adapter -------------------------------------------------------


def test_rentcast_record_maps_to_a_valid_listing():
    adapter = RentCastAdapter(api_key="k")
    listing = Listing.model_validate(adapter._canonical(RAW_RENTCAST))
    assert listing.source == "rentcast"
    assert listing.address == "3821 Hargis St"
    assert listing.unit == "Apt 12"
    assert listing.city == "Austin" and listing.state == "TX"
    assert listing.price == 2100
    assert listing.property_type.value == "single_family"
    assert listing.beds == 3 and listing.baths == 2.0 and listing.sqft == 1428
    assert listing.url.startswith("https://www.google.com/maps/search/")
    assert "Hargis" in listing.url


def test_rentcast_missing_property_type_becomes_other_not_a_reject():
    raw = dict(RAW_RENTCAST)
    del raw["propertyType"]
    listing = Listing.model_validate(RentCastAdapter(api_key="k")._canonical(raw))
    assert listing.property_type.value == "other"


def test_api_key_never_appears_in_repr():
    assert "SECRET" not in repr(RentCastAdapter(api_key="SECRET"))


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.rentcast.io/v1/x", code, "boom", headers or {}, io.BytesIO(b"{}")
    )


def _patch_urlopen(monkeypatch, side_effect):
    import chc_rental.sources.rentcast as mod

    monkeypatch.setattr(mod.urllib.request, "urlopen", side_effect)


@pytest.mark.parametrize(
    "code,expected",
    [(401, SourceAuthError), (403, SourceAuthError), (500, SourceUnavailableError)],
)
def test_http_errors_map_to_the_right_source_error(monkeypatch, code, expected):
    def boom(request, timeout, context):
        raise _http_error(code)

    _patch_urlopen(monkeypatch, boom)
    with pytest.raises(expected):
        RentCastAdapter(api_key="k").fetch_page(SourceQuery("Austin", "TX"), offset=0)


def test_429_carries_retry_after(monkeypatch):
    def boom(request, timeout, context):
        raise _http_error(429, {"Retry-After": "7"})

    _patch_urlopen(monkeypatch, boom)
    with pytest.raises(SourceRateLimitError) as excinfo:
        RentCastAdapter(api_key="k").fetch_page(SourceQuery("Austin", "TX"), offset=0)
    assert excinfo.value.retry_after == 7.0


def test_a_successful_page_returns_records_and_has_more(monkeypatch):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    payload = json.dumps([RAW_RENTCAST]).encode("utf-8")
    _patch_urlopen(monkeypatch, lambda request, timeout, context: FakeResponse(payload))
    adapter = RentCastAdapter(api_key="k", page_size=1)
    records, has_more = adapter.fetch_page(SourceQuery("Austin", "TX"), offset=0)
    assert len(records) == 1 and records[0]["city"] == "Austin"
    assert has_more is True  # a full page means there may be another


def test_parse_retry_after_tolerates_garbage():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("nonsense") is None
    assert _parse_retry_after("12") == 12.0


# --- Zillow managed rental adapter -----------------------------------------


def test_zillow_search_url_is_city_scoped_and_rental_only():
    import urllib.parse

    url = rental_search_url(SourceQuery("Austin", "TX"))
    assert "/austin-tx/rentals/" in url
    encoded = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["searchQueryState"][0]
    state = json.loads(encoded)
    assert state["usersSearchTerm"] == "Austin, TX"
    assert state["filterState"]["fr"]["value"] is True
    assert state["filterState"]["fsba"]["value"] is False


def test_zillow_search_url_includes_actor_required_map_bounds():
    import urllib.parse

    bounds = {"west": -74.05, "east": -73.83, "south": 40.55, "north": 40.74}
    url = rental_search_url(SourceQuery("Brooklyn", "NY"), map_bounds=bounds)
    encoded = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["searchQueryState"][0]
    assert json.loads(encoded)["mapBounds"] == bounds


def test_incremental_zillow_url_pushes_supported_filters_to_the_actor():
    import urllib.parse

    query = SourceQuery(
        "Austin",
        "TX",
        price_min=1500,
        price_max=3000,
        beds_min=2,
        beds_max=3,
        baths_min=1.5,
        property_types=("apartment", "condo"),
    )
    encoded = urllib.parse.parse_qs(
        urllib.parse.urlparse(rental_search_url(query)).query
    )["searchQueryState"][0]
    filters = json.loads(encoded)["filterState"]
    assert filters["price"] == {"min": 1500, "max": 3000}
    assert filters["beds"] == {"min": 2, "max": 3}
    assert filters["baths"] == {"min": 1.5}
    assert filters["isApartment"] == {"value": True}
    assert filters["isCondo"] == {"value": True}
    assert filters["isSingleFamily"] == {"value": False}


def test_zillow_bounds_resolver_maps_nominatim_coordinate_order(monkeypatch):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    payload = json.dumps(
        [{"boundingbox": ["40.5503390", "40.7394340", "-74.0566880", "-73.8329450"]}]
    ).encode("utf-8")
    import chc_rental.sources.zillow as mod

    resolve_map_bounds.cache_clear()
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda request, timeout, context: FakeResponse(payload),
    )
    assert resolve_map_bounds(SourceQuery("Brooklyn", "NY")) == {
        "west": -74.056688,
        "east": -73.832945,
        "south": 40.550339,
        "north": 40.739434,
    }
    resolve_map_bounds.cache_clear()


def test_zillow_rental_maps_to_a_valid_canonical_listing():
    adapter = ZillowRentalAdapter(token="secret")
    listing = Listing.model_validate(adapter._canonical(RAW_ZILLOW_RENTAL))
    assert listing.source == "zillow"
    assert listing.source_listing_id == "123456"
    assert listing.address == "100 Main St" and listing.unit == "4B"
    assert listing.city == "Austin" and listing.state == "TX"
    assert listing.postal_code == "78701"
    assert listing.price == 2200 and listing.property_type.value == "apartment"
    assert listing.beds == 2 and listing.baths == 1.5 and listing.sqft == 900
    assert listing.url.startswith("https://www.zillow.com/homedetails/")


def test_checked_in_zillow_unit_fixture_matches_the_source_contract():
    adapter = ZillowRentalAdapter(token="secret")
    raw = source_fixture("zillow", "unit-rental.json")
    assert adapter._is_rental(raw) is True
    listing = Listing.model_validate(adapter._canonical(raw))
    assert listing.source_listing_id == "123456"
    assert (listing.address, listing.unit, listing.price) == ("100 Main St", "4B", 2200)


@pytest.mark.parametrize(
    "fixture_name",
    ["building-summary.json", "no-results-control.json", "explicit-sale.json"],
)
def test_checked_in_zillow_non_listing_fixtures_are_never_admitted(fixture_name):
    adapter = ZillowRentalAdapter(token="secret")
    assert adapter._is_rental(source_fixture("zillow", fixture_name)) is False


def test_checked_in_malformed_zillow_fixture_is_rejected_not_fabricated():
    adapter = ZillowRentalAdapter(token="secret")
    raw = source_fixture("zillow", "malformed-rental.json")
    assert adapter._is_rental(raw) is True
    with pytest.raises(ValueError) as excinfo:
        Listing.model_validate(adapter._canonical(raw))
    message = str(excinfo.value)
    assert "address" in message and "price" in message


def test_checked_in_rentcast_fixture_matches_the_source_contract():
    raw = source_fixture("rentcast", "active-rental.json")
    listing = Listing.model_validate(RentCastAdapter(api_key="k")._canonical(raw))
    assert listing.source_listing_id == raw["id"]
    assert (listing.address, listing.unit, listing.price) == (
        "3821 Hargis St",
        "Apt 12",
        2100,
    )


def test_zillow_adapter_filters_an_explicit_sale_record(monkeypatch):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    payload = json.dumps([RAW_ZILLOW_RENTAL, dict(RAW_ZILLOW_RENTAL, statusType="FOR_SALE")])
    import chc_rental.sources.zillow as mod

    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda request, timeout, context: FakeResponse(payload.encode("utf-8")),
    )
    records, has_more = ZillowRentalAdapter(
        token="secret", bounds_resolver=lambda query: {
            "west": -98.0, "east": -97.0, "south": 30.0, "north": 31.0
        }
    ).fetch_page(
        SourceQuery("Austin", "TX"), offset=0
    )
    assert len(records) == 1 and records[0]["source"] == "zillow"
    assert has_more is False


def test_zillow_adapter_drops_actor_no_results_control_item(monkeypatch):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import chc_rental.sources.zillow as mod

    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda request, timeout, context: FakeResponse(b'[{"error":"No results found."}]'),
    )
    adapter = ZillowRentalAdapter(
        token="secret", bounds_resolver=lambda query: {
            "west": -98.0, "east": -97.0, "south": 30.0, "north": 31.0
        }
    )
    records, has_more = adapter.fetch_page(SourceQuery("Austin", "TX"), offset=0)
    assert records == [] and has_more is False


def test_zillow_adapter_drops_building_summary_without_unit_level_baths(monkeypatch):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    building = {
        "id": "building-1",
        "statusType": "FOR_RENT",
        "isBuilding": True,
        "addressStreet": "23 Menahan St # 1E",
        "detailUrl": "https://www.zillow.com/apartments/brooklyn-ny/example/",
        "units": [{"price": "$3,000+", "beds": "1"}],
    }
    import chc_rental.sources.zillow as mod

    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda request, timeout, context: FakeResponse(json.dumps([building]).encode()),
    )
    adapter = ZillowRentalAdapter(
        token="secret", bounds_resolver=lambda query: {
            "west": -74.1, "east": -73.8, "south": 40.5, "north": 40.8
        }
    )
    records, _ = adapter.fetch_page(SourceQuery("Brooklyn", "NY"), offset=0)
    assert records == []


def test_zillow_token_never_appears_in_repr():
    assert "SECRET" not in repr(ZillowRentalAdapter(token="SECRET"))


def test_apify_token_does_not_activate_zillow_without_the_settings_flag(tmp_path):
    env = tmp_path / ".env"
    env.write_text("APIFY_TOKEN=SECRET\n", encoding="utf-8")
    adapters, warnings = configured_adapters(Settings(), env_path=str(env))
    assert adapters == [] and warnings == []


def test_explicit_zillow_enable_builds_the_adapter(tmp_path):
    env = tmp_path / ".env"
    env.write_text("APIFY_TOKEN=SECRET\n", encoding="utf-8")
    adapters, warnings = configured_adapters(
        Settings(zillow_enabled=True, zillow_results_limit=5), env_path=str(env)
    )
    assert [adapter.name for adapter in adapters] == ["zillow"]
    assert warnings == []


@pytest.mark.parametrize("code,expected", [(401, SourceAuthError), (429, SourceRateLimitError)])
def test_zillow_http_errors_use_the_shared_taxonomy(monkeypatch, code, expected):
    import chc_rental.sources.zillow as mod

    def boom(request, timeout, context):
        raise _http_error(code, {"Retry-After": "2"})

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    with pytest.raises(expected):
        ZillowRentalAdapter(
            token="secret", bounds_resolver=lambda query: {
                "west": -98.0, "east": -97.0, "south": 30.0, "north": 31.0
            }
        ).fetch_page(SourceQuery("Austin", "TX"), offset=0)


# --- fetch orchestration ----------------------------------------------------


class FakeAdapter:
    """Scriptable adapter: ``pages`` maps (city, offset) -> result or exception."""

    name = "rentcast"

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch_page(self, query, *, offset):
        self.calls.append((query.city, offset))
        result = self.pages[(query.city, offset)]
        if isinstance(result, Exception):
            raise result
        return result


def _store_with_search(store, *, state="TX"):
    person = make_person(111, profile=Profile(searches=[make_search(state=state)]))
    store.save_allowlist(Allowlist(people=[person]))
    return store


def rec(n):
    return {"address": f"{n} Main St"}


def test_fetch_happy_path_caches_and_reports(store):
    _store_with_search(store)
    adapter = FakeAdapter({("Austin", 0): ([rec(1), rec(2)], False)})
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)
    assert report.fetched and report.usable and not report.from_cache
    assert report.requests_used == 1 and report.queries_completed == 1
    assert store.load_cached(FETCH_NOW.date(), "rentcast") == [rec(1), rec(2)]
    assert store.quota_used(FETCH_NOW.date(), "rentcast") == 1


def test_second_run_of_the_day_is_served_from_cache_for_free(store):
    _store_with_search(store)
    adapter = FakeAdapter({("Austin", 0): ([rec(1)], False)})
    fetch_daily(store, adapter, now_utc=FETCH_NOW)
    calls_after_first = list(adapter.calls)
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)
    assert report.from_cache and report.usable
    assert adapter.calls == calls_after_first, "the second run must not hit the network"
    assert store.quota_used(FETCH_NOW.date(), "rentcast") == 1


def test_adding_a_city_invalidates_the_same_days_source_cache(store):
    _store_with_search(store)
    adapter = FakeAdapter(
        {
            ("Austin", 0): ([rec(1)], False),
            ("Dallas", 0): ([rec(2)], False),
        }
    )
    first = fetch_daily(store, adapter, now_utc=FETCH_NOW)
    assert first.records == [rec(1)]

    allowlist = store.load_allowlist()
    allowlist.people[0].profile.searches.append(
        make_search(name="Dallas", city="Dallas", state="TX")
    )
    store.save_allowlist(allowlist)
    second = fetch_daily(store, adapter, now_utc=FETCH_NOW)

    assert second.from_cache is False
    assert second.queries_planned == 2 and second.queries_completed == 2
    assert second.records == [rec(1), rec(2)]
    assert adapter.calls == [("Austin", 0), ("Austin", 0), ("Dallas", 0)]


def test_no_fetch_before_scrape_time(store):
    _store_with_search(store)
    adapter = FakeAdapter({})
    report = fetch_daily(store, adapter, now_utc=BEFORE_SCRAPE)
    assert adapter.calls == [] and not report.usable
    assert any("before scrape time" in w for w in report.warnings)


def test_no_fetchable_searches_costs_nothing_and_is_not_cached(store):
    _store_with_search(store, state=None)
    adapter = FakeAdapter({})
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)
    assert adapter.calls == [] and not report.usable
    assert store.load_cached(FETCH_NOW.date(), "rentcast") is None, (
        "an empty no-search day must not be cached, or adding a search "
        "would not take effect until tomorrow"
    )


def test_pagination_walks_offsets_until_a_short_page(store):
    _store_with_search(store)
    adapter = FakeAdapter(
        {("Austin", 0): ([rec(1), rec(2)], True), ("Austin", 2): ([rec(3)], False)}
    )
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)
    assert [r["address"] for r in report.records] == ["1 Main St", "2 Main St", "3 Main St"]
    assert report.requests_used == 2


def test_budget_exhaustion_truncates_and_still_caches_the_partial(store):
    _store_with_search(store)
    settings = store.load_settings()
    store.save_settings(Settings(**dict(settings.model_dump(), per_source_daily_request_budget=1)))
    adapter = FakeAdapter({("Austin", 0): ([rec(1)], True), ("Austin", 1): ([rec(2)], False)})
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)
    assert report.truncated
    assert [r["address"] for r in report.records] == ["1 Main St"]
    assert store.load_cached(FETCH_NOW.date(), "rentcast") == [rec(1)]


def test_rate_limit_retries_once_and_spends_two_requests(store):
    _store_with_search(store)
    calls = {"n": 0}

    class RateLimitedOnce:
        name = "rentcast"

        def fetch_page(self, query, *, offset):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SourceRateLimitError("slow down", retry_after=3)
            return [rec(1)], False

    waits = []
    report = fetch_daily(store, RateLimitedOnce(), now_utc=FETCH_NOW, sleeper=waits.append)
    assert report.usable and report.requests_used == 2
    assert waits == [3]
    assert store.quota_used(FETCH_NOW.date(), "rentcast") == 2


def test_a_dead_source_yields_an_unusable_report(store):
    _store_with_search(store)
    adapter = FakeAdapter({("Austin", 0): SourceUnavailableError("down")})
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)
    assert report.fetched and not report.usable
    assert report.queries_completed == 0 and report.errors


def test_auth_failure_aborts_the_whole_sweep(store):
    _store_with_search(store)
    adapter = FakeAdapter({("Austin", 0): SourceAuthError("bad key")})
    with pytest.raises(SourceAuthError):
        fetch_daily(store, adapter, now_utc=FETCH_NOW)


def test_multi_source_pool_keeps_one_source_when_another_auth_fails(store):
    _store_with_search(store)
    healthy = FakeAdapter({("Austin", 0): ([rec(1)], False)})

    class BrokenZillow:
        name = "zillow"

        def fetch_page(self, query, *, offset):
            raise SourceAuthError("bad Apify token")

    pool = fetch_many_daily(store, [healthy, BrokenZillow()], now_utc=FETCH_NOW)
    assert pool.usable is True
    assert pool.complete is False
    assert pool.records == [rec(1)]
    assert [report.source for report in pool.sources] == ["rentcast", "zillow"]
    assert pool.sources[1].errors == ["bad Apify token"]


def test_truncated_or_errored_usable_source_makes_pool_incomplete():
    for report in (
        FetchReport(source="rentcast", from_cache=True, truncated=True),
        FetchReport(source="rentcast", from_cache=True, errors=["partial city failure"]),
    ):
        pool = PoolFetchReport(sources=[report], records=[rec(1)])
        assert pool.usable is True
        assert pool.complete is False


class NeverEndingAdapter:
    """A dense market: every page is full and claims there is another.

    Models Brooklyn — thousands of active listings. The sweep must stop at the
    page cap and USE what it gathered, not discard it as a failure.
    """

    name = "rentcast"

    def __init__(self):
        self.calls = 0

    def fetch_page(self, query, *, offset):
        self.calls += 1
        return [rec(offset)], True  # always "one more page"


def test_page_cap_keeps_the_slice_instead_of_discarding_it(store):
    """Regression for the 2026-08-11 Brooklyn bug: hitting the page cap wiped
    every fetched listing and reported 'no usable pool', so nothing pushed."""
    import chc_rental.fetch as fetch_mod

    _store_with_search(store)
    # Budget must not be the limiting factor; we want the PAGE cap to trigger.
    settings = store.load_settings()
    store.save_settings(
        Settings(**dict(settings.model_dump(), per_source_daily_request_budget=999,
                        global_daily_request_budget=999))
    )
    adapter = NeverEndingAdapter()
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)

    assert report.usable, "a page-capped fetch must remain usable"
    assert report.queries_completed == 1
    assert len(report.records) == fetch_mod.MAX_PAGES_PER_QUERY
    assert adapter.calls == fetch_mod.MAX_PAGES_PER_QUERY, "must stop at the cap, not loop forever"
    assert report.truncated is True
    assert report.errors == [], "hitting the cap is a warning, not an error"
    assert any("page cap" in w for w in report.warnings)
    assert store.load_cached(FETCH_NOW.date(), "rentcast") == report.records

    calls_after_fetch = adapter.calls
    cached_pool = fetch_many_daily(store, [adapter], now_utc=FETCH_NOW)
    assert adapter.calls == calls_after_fetch
    assert cached_pool.sources[0].from_cache is True
    assert cached_pool.sources[0].truncated is True
    assert cached_pool.complete is False


def test_one_citys_page_cap_does_not_starve_other_cities(store):
    """A dense city must not stop the sweep before other cities are fetched."""
    from chc_rental.models import Allowlist, Profile

    a = make_person(111, profile=Profile(searches=[make_search(city="Brooklyn", state="NY")]))
    b = make_person(222, profile=Profile(searches=[make_search(city="Austin", state="TX")]))
    store.save_allowlist(Allowlist(people=[a, b]))
    store.save_settings(
        Settings(**dict(store.load_settings().model_dump(),
                        per_source_daily_request_budget=999, global_daily_request_budget=999))
    )

    class TwoCityAdapter:
        name = "rentcast"

        def fetch_page(self, query, *, offset):
            if query.city == "Brooklyn":
                return [rec(offset)], True  # never terminates -> hits page cap
            return [{"address": "austin one"}], False  # terminates immediately

    report = fetch_daily(store, TwoCityAdapter(), now_utc=FETCH_NOW)
    assert report.queries_completed == 2, "Austin must still be fetched after Brooklyn caps out"
    assert any(r.get("address") == "austin one" for r in report.records)


def test_is_scrape_time_uses_the_configured_zone():
    settings = Settings()  # 08:00 America/New_York
    assert is_scrape_time(settings, BEFORE_SCRAPE) is False
    assert is_scrape_time(settings, FETCH_NOW) is True
