"""Fetch layer: planner dedup, RentCast mapping, budget/cache/retry orchestration.

No test performs network I/O: the adapter's HTTP method is exercised through a
fake `urlopen`, and `fetch_daily` through fake adapters.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from chc_rental.fetch import (
    FetchReport,
    PoolFetchReport,
    fetch_daily,
    fetch_many_daily,
    is_scrape_time,
    load_daily_cached,
    scrape_day,
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
from chc_rental.sources.zillow import (
    ZillowRentalAdapter,
    rental_search_url,
    resolve_map_bounds,
)

from tests.conftest import make_person, make_search
from tests.test_apify_run_lifecycle import FakeClient

# 12:00 UTC = 08:00 America/New_York in January — exactly the default scrape time.
FETCH_NOW = datetime(2026, 1, 15, 13, 5, tzinfo=timezone.utc)
BEFORE_SCRAPE = datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)  # 06:00 New York

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

# The actor switched to this shape on 2026-09-02 with no announcement: address
# and price moved into nested objects, beds/baths gained full-word field
# names, the detail link moved to `propertyUrl`, and status became camelCase
# (`forRent` instead of `FOR_RENT`). The old shape above kept validating fine
# against the adapter's own field names while every real run silently mapped
# to blanks, so both shapes are pinned here.
RAW_ZILLOW_RENTAL_NEW_SCHEMA = {
    "zpid": "2066340055",
    "propertyUrl": "https://www.zillow.com/homedetails/6631-Duryea-Ct-2F-Brooklyn-NY-11219/2066340055_zpid/",
    "listingStatus": "forRent",
    "listingAddress": {
        "street": "6631 Duryea Ct #2F",
        "unit": "# 2F",
        "city": "Brooklyn",
        "state": "NY",
        "zipCode": "11219",
        "full": "6631 Duryea Ct #2F, Brooklyn, NY 11219",
    },
    "listingPrice": {"amount": 2850, "currency": "USD", "formatted": "$2,850/mo"},
    "homeType": "APARTMENT",
    "bedrooms": 2,
    "bathrooms": 2,
    "livingArea": 1200,
}

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sources"


def source_fixture(source: str, name: str) -> dict:
    return json.loads((FIXTURE_ROOT / source / name).read_text(encoding="utf-8"))


# --- planner ----------------------------------------------------------------


def test_two_people_watching_one_city_produce_one_query():
    a = make_person(111, profile=Profile(searches=[make_search(state="TX")]))
    b = make_person(222, profile=Profile(searches=[make_search(name="Other", state="tx")]))
    queries, warnings = plan_queries(Allowlist(people=[a, b]))
    assert len(queries) == 1
    assert (queries[0].city, queries[0].state) == ("Austin", "TX")
    assert warnings == []


def test_query_count_never_exceeds_the_number_of_watched_cities():
    """One planned query is one paid Apify actor run, so the query COUNT is the
    monthly bill. On 2026-08-16 a planner split each city into 3 price bands x 2
    bed groups; Zillow spend went 1 -> 5 runs/day and exhausted the Apify free
    tier on 2026-08-20, taking the 07:00 push down for every recipient. Widening
    or subdividing a city's filters is free; adding a query is not."""
    wide = make_person(
        111,
        profile=Profile(
            searches=[
                make_search(name="Cheap studio", state="TX", price_min=500,
                            price_max=1500, bed_min=0, bed_max=1),
                make_search(name="Mid two-bed", state="TX", price_min=1500,
                            price_max=4000, bed_min=2, bed_max=3),
                make_search(name="Luxury house", state="TX", price_min=4000,
                            price_max=12000, bed_min=4, bed_max=6),
            ]
        ),
    )
    other_city = make_person(
        222,
        profile=Profile(searches=[make_search(name="Dallas", city="Dallas", state="TX")]),
    )
    queries, warnings = plan_queries(Allowlist(people=[wide, other_city]))

    cities = {(query.city.lower(), query.state) for query in queries}
    assert len(queries) == len(cities) == 2, (
        "each watched city must cost exactly one source request; "
        f"got {len(queries)} queries for {len(cities)} cities"
    )
    assert warnings == []

    austin = next(query for query in queries if query.city == "Austin")
    assert (austin.price_min, austin.price_max) == (500, 12000)
    assert (austin.beds_min, austin.beds_max) == (0, 6)


def test_many_searches_in_one_city_still_spend_one_request(store):
    """The end-to-end form of the cost invariant, measured at the budget ledger."""
    store.save_allowlist(
        Allowlist(
            people=[
                make_person(
                    111,
                    profile=Profile(
                        searches=[
                            make_search(name=f"Search {index}", state="TX",
                                        price_min=1000 * index,
                                        price_max=1000 * index + 5000)
                            for index in range(1, 6)
                        ]
                    ),
                )
            ]
        )
    )
    adapter = FakeAdapter({("Austin", 0): ([rec(1)], False)})
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)

    assert report.queries_planned == 1
    assert report.requests_used == 1
    assert len(adapter.calls) == 1
    assert not report.truncated and report.errors == []
    assert store.quota_used(FETCH_NOW.date(), "rentcast") == 1


def test_query_carries_the_search_filter_envelope():
    """Regression for the 2026-08-13 report: the daily Zillow scrape ignored
    the dashboard filters, so scraped listings did not fit the search."""
    person = make_person(
        111,
        profile=Profile(searches=[make_search(
            state="TX", price_min=2000, price_max=4000, bed_min=2, bed_max=3,
            bath_min=2.0, property_types=["apartment", "condo"],
        )]),
    )
    (query,), _ = plan_queries(Allowlist(people=[person]))
    assert (query.price_min, query.price_max) == (2000, 4000)
    assert (query.beds_min, query.beds_max) == (2, 3)
    assert query.baths_min == 2.0
    assert query.property_types == ("apartment", "condo")


def test_envelope_is_a_superset_of_every_search_in_the_city():
    """Two different searches in one city must yield one query wide enough to
    include what either would match — precise narrowing happens locally."""
    narrow = make_search(
        name="narrow", state="TX", price_min=2000, price_max=3000,
        bed_min=2, bed_max=2, bath_min=2.0, property_types=["apartment"],
    )
    wide = make_search(
        name="wide", state="TX", price_min=1000, price_max=5000,
        bed_min=1, bed_max=4, bath_min=1.0, property_types=["condo", "house"],
    )
    person = make_person(111, profile=Profile(searches=[narrow, wide]))
    (query,), _ = plan_queries(Allowlist(people=[person]))
    assert (query.price_min, query.price_max) == (1000, 5000)
    assert (query.beds_min, query.beds_max) == (1, 4)
    assert query.baths_min == 1.0  # smallest floor, so neither search is fetched out
    # "house" maps to the Zillow single_family group; union across both searches
    assert set(query.property_types) == {"apartment", "condo", "single_family"}


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


# --- shared HTTP-error helper (used by the Zillow adapter tests) -----------


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.example.com/v1/x", code, "boom", headers or {}, io.BytesIO(b"{}")
    )


# --- Zillow managed rental adapter -----------------------------------------


def test_zillow_search_url_is_city_scoped_and_rental_only():
    import urllib.parse

    url = rental_search_url(SourceQuery("Austin", "TX"))
    assert "/austin-tx/rentals/" in url
    encoded = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["searchQueryState"][0]
    state = json.loads(encoded)
    assert state["usersSearchTerm"] == "Austin, TX"
    assert state["filterState"]["sortSelection"] == {"value": "days"}
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
    assert filters["mp"] == {"min": 1500, "max": 3000}
    assert filters["beds"] == {"min": 2, "max": 3}
    assert filters["baths"] == {"min": 1.5}
    assert filters["isApartment"] == {"value": True}
    assert filters["isCondo"] == {"value": True}
    assert filters["isSingleFamily"] == {"value": False}


def test_rent_envelope_uses_mp_and_never_the_sale_price_filter():
    """Rent must be sent as `mp`; `price` is Zillow's for-sale home-value band.

    Sending the rent envelope as `price` on a /rentals/ URL is silently ignored,
    which is how 64% of every paid result slot went to listings outside the
    envelope through 2026-08-28. Re-adding `price` is worse than a no-op: if
    Zillow honored it as home value, a $5k-$10k band would match no property at
    all and the daily pool would go quietly empty rather than loudly fail.
    """
    import urllib.parse

    query = SourceQuery("Brooklyn", "NY", price_min=5000, price_max=10000)
    encoded = urllib.parse.parse_qs(
        urllib.parse.urlparse(rental_search_url(query)).query
    )["searchQueryState"][0]
    filters = json.loads(encoded)["filterState"]
    assert filters["mp"] == {"min": 5000, "max": 10000}
    assert "price" not in filters


def test_bounds_are_geocoded_once_per_city_not_once_per_query(monkeypatch):
    """A city's location does not depend on a price filter. Caching on the whole
    SourceQuery made every extra query over one city pay for its own Nominatim
    lookup, against an endpoint that asks for one request per second."""
    calls = []

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

    def fake_urlopen(request, timeout, context):
        calls.append(request.full_url)
        return FakeResponse(payload)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    for price_min, price_max in ((5000, 6666), (6666, 8333), (8333, 10000)):
        resolve_map_bounds(
            SourceQuery("Brooklyn", "NY", price_min=price_min, price_max=price_max)
        )
    assert len(calls) == 1, f"one city must cost one geocode, got {len(calls)}"

    resolve_map_bounds(SourceQuery("Queens", "NY"))
    assert len(calls) == 2, "a different city is still a different lookup"


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


def test_zillow_rental_new_schema_maps_to_a_valid_canonical_listing():
    """Pins the 2026-09-02 actor schema switch.

    Verified live against the actor on 2026-09-07: `_is_rental` accepted every
    result on both old and new field names, but `_canonical` only knew the old
    ones, so every real listing mapped to blanks and failed `Listing`
    validation. Five straight days of zero deliveries, all silently paid for.
    """
    adapter = ZillowRentalAdapter(token="secret")
    assert adapter._is_rental(RAW_ZILLOW_RENTAL_NEW_SCHEMA) is True
    listing = Listing.model_validate(adapter._canonical(RAW_ZILLOW_RENTAL_NEW_SCHEMA))
    assert listing.source == "zillow"
    assert listing.source_listing_id == "2066340055"
    assert listing.address == "6631 Duryea Ct" and listing.unit == "# 2F"
    assert listing.city == "Brooklyn" and listing.state == "NY"
    assert listing.postal_code == "11219"
    assert listing.price == 2850 and listing.property_type.value == "apartment"
    assert listing.beds == 2 and listing.baths == 2 and listing.sqft == 1200
    assert listing.url.startswith("https://www.zillow.com/homedetails/")


def test_zillow_adapter_drops_new_schema_building_summary_without_isbuilding_flag():
    """The new building-card shape carries no `isBuilding` flag at all — only a
    non-empty `units` price-range list and a `/b/` (not `/homedetails/`)
    `propertyUrl`. Must still be excluded, same as the old-shape card below.
    """
    building = {
        "zpid": "40.65784--73.95526",
        "listingStatus": "forRent",
        "propertyUrl": "https://www.zillow.com/b/151-hawthorne-st-brooklyn-ny-97cvPT/",
        "listingAddress": {"street": "151 Hawthorne St", "city": "Brooklyn", "state": "NY"},
        "units": [{"price": "$3,000+", "beds": "0", "roomForRent": False}],
    }
    records, _ = zillow_adapter(FakeClient(raw=[building])).fetch_page(
        SourceQuery("Brooklyn", "NY"), offset=0
    )
    assert records == []


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


BOUNDS_AUSTIN = {"west": -98.0, "east": -97.0, "south": 30.0, "north": 31.0}


def zillow_adapter(client, **overrides):
    """Adapter wired to a fake Apify client and a no-op sleeper.

    Every adapter test MUST pass a client. Without one the adapter builds a real
    ApifyClient and the run reaches api.apify.com for real — which is exactly
    what these tests started doing when the adapter moved off the monkeypatched
    sync endpoint.
    """
    return ZillowRentalAdapter(
        token="secret",
        bounds_resolver=lambda query: BOUNDS_AUSTIN,
        client=client,
        sleeper=lambda seconds: None,
        **overrides,
    )


def test_zillow_adapter_filters_an_explicit_sale_record():
    client = FakeClient(raw=[RAW_ZILLOW_RENTAL, dict(RAW_ZILLOW_RENTAL, statusType="FOR_SALE")])
    records, has_more = zillow_adapter(client).fetch_page(
        SourceQuery("Austin", "TX"), offset=0
    )
    assert len(records) == 1 and records[0]["source"] == "zillow"
    assert has_more is False


def test_zillow_adapter_drops_actor_no_results_control_item():
    client = FakeClient(raw=[{"error": "No results found."}])
    records, has_more = zillow_adapter(client).fetch_page(
        SourceQuery("Austin", "TX"), offset=0
    )
    assert records == [] and has_more is False


def test_zillow_adapter_drops_building_summary_without_unit_level_baths():
    building = {
        "id": "building-1",
        "statusType": "FOR_RENT",
        "isBuilding": True,
        "addressStreet": "23 Menahan St # 1E",
        "detailUrl": "https://www.zillow.com/apartments/brooklyn-ny/example/",
        "units": [{"price": "$3,000+", "beds": "1"}],
    }
    records, _ = zillow_adapter(FakeClient(raw=[building])).fetch_page(
        SourceQuery("Brooklyn", "NY"), offset=0
    )
    assert records == []


def test_waiting_for_a_run_never_starts_a_second_one():
    """The double-billing regression, pinned.

    Holding the run open on run-sync-get-dataset-items meant a dropped
    connection made the fetch loop buy a SECOND run while the first finished
    and billed anyway: 2026-08-23 and 2026-08-28 each show two billed
    25-result runs for one day's listings. Polling must never re-start.
    """
    client = FakeClient(start_status="RUNNING", poll_status="SUCCEEDED")
    zillow_adapter(client).fetch_page(SourceQuery("Austin", "TX"), offset=0)
    assert client.start_calls == 1, "polling a run must never start another paid run"
    assert client.poll_calls >= 1


def test_a_run_that_never_finishes_abandons_the_wait_not_the_run():
    """Giving up on the wait must not buy the listings a second time."""
    client = FakeClient(start_status="RUNNING", poll_status="RUNNING")
    with pytest.raises(SourceUnavailableError, match="still RUNNING"):
        zillow_adapter(client, timeout=10.0).fetch_page(
            SourceQuery("Austin", "TX"), offset=0
        )
    assert client.start_calls == 1


def test_a_failed_run_reports_the_actors_own_reason():
    """The actor rejects a URL it cannot parse, and says why. Surfacing that
    verbatim is what identified `doz` needing an integer value on 2026-08-29
    ("No valid search URLs found on input")."""
    client = FakeClient(start_status="RUNNING", poll_status="FAILED")
    with pytest.raises(SourceUnavailableError, match="FAILED"):
        zillow_adapter(client).fetch_page(SourceQuery("Austin", "TX"), offset=0)


@pytest.mark.parametrize(
    "error", [SourceAuthError("bad token"), SourceRateLimitError("slow down")]
)
def test_apify_client_errors_reach_the_fetch_loop_unchanged(error):
    """The fetch loop reacts differently to each taxonomy member — auth aborts
    the sweep, rate limit retries once — so the adapter must not flatten them."""

    class Raising:
        def start_actor(self, actor, payload, *, max_total_charge_usd):
            raise error

    with pytest.raises(type(error)):
        zillow_adapter(Raising()).fetch_page(SourceQuery("Austin", "TX"), offset=0)


def test_the_run_is_started_with_the_configured_charge_ceiling():
    """Last line of defence on a per-result actor: even if the results limit is
    somehow ignored, the run cannot bill past this."""

    class Recording(FakeClient):
        charge = None

        def start_actor(self, actor, payload, *, max_total_charge_usd):
            Recording.charge = max_total_charge_usd
            return super().start_actor(actor, payload, max_total_charge_usd=max_total_charge_usd)

    zillow_adapter(Recording()).fetch_page(SourceQuery("Austin", "TX"), offset=0)
    assert Recording.charge == 0.25

    zillow_adapter(Recording(), max_charge_usd=0.0).fetch_page(
        SourceQuery("Austin", "TX"), offset=0
    )
    assert Recording.charge is None, "a zero ceiling means unset, not free"


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


def test_auth_failure_aborts_the_sweep_and_reports_it_without_raising(store):
    """The sweep still stops, but the report survives to carry the reason.

    It used to propagate, and `fetch_many_daily` rebuilt a blank report — which
    reported requests_used=0 for reservations that had already been spent."""
    _store_with_search(store)
    adapter = FakeAdapter({("Austin", 0): SourceAuthError("bad key")})
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)
    assert report.fetched and not report.usable
    assert report.queries_completed == 0
    assert report.errors == ["bad key"]


def test_a_rejected_credential_does_not_consume_the_daily_budget(store):
    """Regression for 2026-08-20: five 403s ate the whole Zillow budget in fifty
    minutes, after which every tick blamed the budget instead of the token."""
    _store_with_search(store)
    adapter = FakeAdapter({("Austin", 0): SourceAuthError("HTTP 403: cap exceeded")})

    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)

    assert report.requests_used == 0
    assert store.quota_used(FETCH_NOW.date(), "rentcast") == 0, (
        "a request the source refused outright costs nothing and must not be banked"
    )


def test_a_rejected_credential_is_not_retried_for_the_rest_of_the_day(store):
    _store_with_search(store)
    adapter = FakeAdapter({("Austin", 0): SourceAuthError("HTTP 403: cap exceeded")})
    fetch_daily(store, adapter, now_utc=FETCH_NOW)
    assert len(adapter.calls) == 1

    later = fetch_daily(store, adapter, now_utc=FETCH_NOW.replace(hour=14))
    assert len(adapter.calls) == 1, "the day breaker must stop the retry loop"
    assert not later.usable
    # The error stays byte-identical to the failure that tripped the breaker, so
    # the run summary keeps one signature and the operator alert pages once per
    # streak. The suppression itself is reported as a warning.
    assert later.errors == ["HTTP 403: cap exceeded"]
    assert any("not retried" in note for note in later.warnings)

    # The block is scoped to the scrape day, so tomorrow tries again by itself.
    tomorrow = FETCH_NOW.replace(day=FETCH_NOW.day + 1)
    adapter.pages[("Austin", 0)] = ([rec(1)], False)
    recovered = fetch_daily(store, adapter, now_utc=tomorrow)
    assert recovered.usable and recovered.records == [rec(1)]


def test_a_partial_sweep_interrupted_by_auth_keeps_what_it_paid_for(store):
    person = make_person(
        111,
        profile=Profile(
            searches=[
                make_search(name="Austin", city="Austin", state="TX"),
                make_search(name="Dallas", city="Dallas", state="TX"),
            ]
        ),
    )
    store.save_allowlist(Allowlist(people=[person]))
    adapter = FakeAdapter(
        {
            ("Austin", 0): ([rec(1)], False),
            ("Dallas", 0): SourceAuthError("token revoked mid-sweep"),
        }
    )

    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)

    assert report.records == [rec(1)]
    assert report.queries_completed == 1
    assert report.requests_used == 1, "the Austin page was really paid for"
    assert store.quota_used(FETCH_NOW.date(), "rentcast") == 1
    assert report.errors == ["token revoked mid-sweep"]


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


def test_scrape_day_rolls_over_at_local_midnight_not_utc_midnight():
    settings = Settings(scrape_timezone="America/New_York")
    assert scrape_day(
        settings, datetime(2026, 8, 14, 3, 59, tzinfo=timezone.utc)
    ) == date(2026, 8, 13)
    assert scrape_day(
        settings, datetime(2026, 8, 14, 4, 0, tzinfo=timezone.utc)
    ) == date(2026, 8, 14)


def test_legacy_utc_dated_evening_cache_cannot_block_morning_scrape(store):
    _store_with_search(store)
    store.save_settings(
        Settings(scrape_time="06:00", scrape_timezone="America/New_York")
    )
    day = date(2026, 8, 14)
    path = store.cache_raw(
        day,
        "rentcast",
        [rec(99)],
        metadata={
            "query_scope": [{"city": "austin", "state": "TX"}],
            "queries_planned": 1,
            "queries_completed": 1,
        },
    )
    # This file was written at 20:01 New York on August 13 but filed under the
    # already-rolled UTC date August 14 by the old runner.
    old_utc = datetime(2026, 8, 14, 0, 1, tzinfo=timezone.utc).timestamp()
    os.utime(path, (old_utc, old_utc))

    before = datetime(2026, 8, 14, 9, 59, tzinfo=timezone.utc)  # 05:59 NY
    waiting = fetch_daily(store, FakeAdapter({}), now_utc=before)
    assert waiting.usable is False

    at_scrape = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)  # 06:00 NY
    adapter = FakeAdapter({("Austin", 0): ([rec(1)], False)})
    fetched = fetch_daily(store, adapter, now_utc=at_scrape)
    assert fetched.fetched is True and fetched.records == [rec(1)]
    metadata = store.load_cache_metadata(day, "rentcast")
    assert metadata["scrape_day"] == "2026-08-14"
    assert metadata["scrape_timezone"] == "America/New_York"


def test_delivery_cache_loader_never_needs_an_adapter(store):
    _store_with_search(store)
    settings = Settings(scrape_timezone="America/New_York")
    store.save_settings(settings)
    now = datetime(2026, 1, 15, 20, tzinfo=timezone.utc)
    day = scrape_day(settings, now)
    store.cache_raw(
        day,
        "zillow",
        [rec(1)],
        metadata={
            "query_scope": [{"city": "austin", "state": "TX"}],
            "scrape_day": day.isoformat(),
            "scrape_timezone": settings.scrape_timezone,
            "queries_planned": 1,
            "queries_completed": 1,
        },
    )
    report = load_daily_cached(store, "zillow", now_utc=now)
    assert report.from_cache is True and report.records == [rec(1)]


def _priced(price, *, city="Austin", state="TX", beds=2):
    return {"address": f"{price} Main St", "city": city, "state": state,
            "price": price, "beds": beds}


def test_in_city_results_outside_the_rent_envelope_are_counted_and_warned(store):
    """A source filter that stops applying must not fail silently.

    Nothing downstream errors when the provider ignores our filter — local
    matching just rejects more listings, quietly, while every rejected record
    was still paid for. This is the tripwire: the share is recorded on the run
    and in the cache metadata, and crossing the threshold warns.
    """
    _store_with_search(store)  # envelope: Austin TX, $1,000-$3,000, 1-3 beds
    adapter = FakeAdapter(
        {("Austin", 0): ([_priced(1500), _priced(500), _priced(9000)], False)}
    )
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)

    assert any("outside its rent/bed envelope" in w for w in report.warnings)
    metadata = store.load_cache_metadata(FETCH_NOW.date(), "rentcast")
    assert metadata["envelope_audit"] == {
        "comparable": 3, "outside_city": 0, "outside_filters": 2,
    }
    assert metadata["envelope_miss_share"] == pytest.approx(2 / 3)


def test_out_of_city_results_are_counted_apart_and_do_not_trip_the_warning(store):
    """The actor needs a RECTANGULAR map bound and cities are not rectangles, so
    a known share of every run is in a city nobody watches — 27% of a Brooklyn
    run measured 2026-08-29 (Lower Manhattan, Jersey City, western Queens).

    That is a standing cost, not a regression. Folded into the same number it
    would sit permanently above the threshold and drown the signal the warning
    exists to carry.
    """
    _store_with_search(store)
    adapter = FakeAdapter(
        {("Austin", 0): ([_priced(1500), _priced(2000, city="Round Rock")], False)}
    )
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)

    assert not any("rent/bed envelope" in w for w in report.warnings)
    metadata = store.load_cache_metadata(FETCH_NOW.date(), "rentcast")
    assert metadata["envelope_audit"] == {
        "comparable": 2, "outside_city": 1, "outside_filters": 0,
    }


def test_a_correctly_filtered_pool_raises_no_envelope_warning(store):
    _store_with_search(store)
    adapter = FakeAdapter(
        {("Austin", 0): ([_priced(1500), _priced(2000), _priced(2900)], False)}
    )
    report = fetch_daily(store, adapter, now_utc=FETCH_NOW)

    assert not any("rent/bed envelope" in w for w in report.warnings)
    metadata = store.load_cache_metadata(FETCH_NOW.date(), "rentcast")
    assert metadata["envelope_miss_share"] == 0.0


def test_unpriced_results_are_excluded_from_the_envelope_measure(store):
    """Building cards and error markers carry no rent; they are a separate leak.

    Counting them as envelope misses would blame the price filter for records
    it never had a chance to exclude, and mask a real filter regression behind
    a number that never drops.
    """
    _store_with_search(store)
    adapter = FakeAdapter(
        {("Austin", 0): ([_priced(1500), {"address": "No price St"}], False)}
    )
    fetch_daily(store, adapter, now_utc=FETCH_NOW)

    metadata = store.load_cache_metadata(FETCH_NOW.date(), "rentcast")
    assert metadata["envelope_audit"] == {
        "comparable": 1, "outside_city": 0, "outside_filters": 0,
    }
