"""Owner-approved, terms-flagged Zillow rental collection through Apify.

This adapter never talks to Zillow directly. It submits one rental search URL
for one ``(city, state)`` query to a pinned managed actor and maps the returned
search cards into CHC_Rental's canonical listing shape. One ``fetch_page`` call
is one actor run; the actor returns a bounded batch, so ``has_more`` is always
false.

The run is started ASYNCHRONOUSLY and polled, rather than held open on
``run-sync-get-dataset-items``. The sync endpoint keeps one HTTP connection
open for the whole run, and when that connection dropped the retry bought a
SECOND run while the first went on to finish and bill anyway: 2026-08-23 and
2026-08-28 each show two billed 25-result runs for one day's data. The actor
bills per result, so that mistake scales with ``results_limit``. Polling costs
nothing and a dropped poll simply polls again.

The integration is opt-in in ``Settings.zillow_enabled``. Merely placing an
Apify token in ``.env`` cannot activate it.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Optional

from chc_rental.envfile import read_env_key
from chc_rental.net import ssl_context
from chc_rental.sources.apify import ApifyClient
from chc_rental.sources.base import (
    SourceQuery,
    SourceUnavailableError,
)

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
ZILLOW = "https://www.zillow.com"
SOURCE_NAME = "zillow"

_RENTAL_STATUSES = {
    "FOR_RENT",
    "FOR RENT",
    "FOR_RENT_BY_AGENT",
    "FOR_RENT_BY_OWNER",
}

# Same set, underscores/spaces stripped, so both the older ``statusType``
# shape (``FOR_RENT``) and the newer ``listingStatus`` shape (``forRent``)
# match without keeping two literal tables in sync by hand.
_RENTAL_STATUSES_NORMALIZED = {s.replace("_", "").replace(" ", "") for s in _RENTAL_STATUSES}

_PROPERTY_TYPES = {
    "APARTMENT": "apartment",
    "CONDO": "condo",
    "TOWNHOUSE": "townhouse",
    "SINGLE_FAMILY": "single_family",
    "MULTI_FAMILY": "multi_family",
    "MANUFACTURED": "manufactured",
    "LOT": "land",
}

_QUERY_TYPE_FILTERS = {
    "apartment": "isApartment",
    "condo": "isCondo",
    "townhouse": "isTownhouse",
    "single_family": "isSingleFamily",
    "multi_family": "isMultiFamily",
    "manufactured": "isManufactured",
    "land": "isLotLand",
}

_UNIT_AT_END = re.compile(
    r"\s+(?:(?:apt|apartment|unit|suite|ste)\s+|#)([A-Za-z0-9-]+)\s*$",
    re.IGNORECASE,
)


def load_apify_token(env_path: str = ".env") -> Optional[str]:
    return read_env_key(env_path, "APIFY_TOKEN")


def rental_search_url(
    query: SourceQuery,
    *,
    map_bounds: Optional[dict[str, float]] = None,
    days_on_zillow: Optional[int] = None,
) -> str:
    """Build one stable, rental-only Zillow search URL for the actor.

    The managed actor requires a map-backed Zillow URL. A city term alone can
    look valid in a browser but produces an Apify ``No results found`` marker.
    ``fetch_page`` therefore resolves and supplies bounds before starting the
    actor; keeping this parameter optional makes the pure URL builder useful in
    tests and diagnostics.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", f"{query.city}-{query.state}".lower()).strip("-")
    filter_state = {
        "sortSelection": {"value": "days"},
        "fr": {"value": True},
        "fsba": {"value": False},
        "fsbo": {"value": False},
        "nc": {"value": False},
        "fore": {"value": False},
        "cmsn": {"value": False},
        "auc": {"value": False},
    }
    if query.price_min is not None or query.price_max is not None:
        # RENT goes in `mp` (monthly payment), NOT `price`. On a /rentals/ URL
        # Zillow reads `price` as the for-sale HOME VALUE band and ignores it,
        # so a rent envelope sent as `price` never reaches the search at all.
        #
        # Measured 2026-08-29, same city/moment/sort, 10 results each:
        #   price -> 4/7 priced results inside a $5,000-$10,000 envelope (57%),
        #            leaking $3,970 / $4,500 / $4,695
        #   mp    -> 7/7 (100%), floor exactly $5,000
        # The `mp` run also surfaced $8,000 and $9,000 listings the `price` run
        # never reached: the slots wasted on sub-envelope rents were clipping
        # real candidates off the end of a newest-first window.
        #
        # `price` is deliberately NOT also sent. It is not harmlessly ignored in
        # every case: were Zillow to honor it as home value on a rentals URL, a
        # $5,000-$10,000 home-value band would match nothing and the daily pool
        # would silently go empty.
        rent: dict[str, int] = {}
        if query.price_min is not None:
            rent["min"] = int(query.price_min)
        if query.price_max is not None:
            rent["max"] = int(query.price_max)
        filter_state["mp"] = rent
    if query.beds_min is not None or query.beds_max is not None:
        beds: dict[str, int] = {}
        if query.beds_min is not None:
            beds["min"] = int(query.beds_min)
        if query.beds_max is not None:
            beds["max"] = int(query.beds_max)
        filter_state["beds"] = beds
    if query.baths_min is not None:
        minimum = float(query.baths_min)
        filter_state["baths"] = {
            "min": int(minimum) if minimum.is_integer() else minimum
        }
    if query.property_types:
        selected = set(query.property_types)
        for property_type, filter_name in _QUERY_TYPE_FILTERS.items():
            filter_state[filter_name] = {"value": property_type in selected}
    if days_on_zillow is not None:
        # Bounds the window by RECENCY instead of by our own resultsLimit. Under
        # per-result billing that is what stops a large limit from re-buying
        # listings the seen ledger already holds: if only twelve in-band rentals
        # were listed in the window, the run returns twelve and bills for twelve.
        # INTEGER, not a string. Verified against the actor 2026-08-29:
        # {"value": 1} is accepted and filters; {"value": "1"} makes the actor
        # reject the whole URL with "No valid search URLs found on input".
        filter_state["doz"] = {"value": int(days_on_zillow)}
    state = {
        "usersSearchTerm": f"{query.city}, {query.state}",
        "filterState": filter_state,
        "isListVisible": True,
        "isMapVisible": True,
    }
    if map_bounds is not None:
        state["mapBounds"] = map_bounds
    encoded = urllib.parse.quote(json.dumps(state, separators=(",", ":")))
    return f"{ZILLOW}/{slug}/rentals/?searchQueryState={encoded}"


@lru_cache(maxsize=128)
def _city_map_bounds(city: str, state: str, timeout: float) -> dict[str, float]:
    """Cached geocode for one (city, state). See ``resolve_map_bounds``."""
    params = urllib.parse.urlencode(
        {
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "us",
            "city": city,
            "state": state,
        }
    )
    request = urllib.request.Request(
        f"{NOMINATIM_SEARCH_URL}?{params}",
        headers={
            "Accept": "application/json",
            "User-Agent": "CHC_Rental/1.0 (local rental notification service)",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=min(float(timeout), 20.0), context=ssl_context()
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise SourceRateLimitError(
                "Zillow bounds geocoder rate limit (HTTP 429)",
                retry_after=_parse_retry_after(exc.headers.get("Retry-After")),
            ) from None
        raise SourceUnavailableError(
            f"Zillow bounds geocoder HTTP {exc.code}"
        ) from None
    except urllib.error.URLError as exc:
        raise SourceUnavailableError(
            f"Zillow bounds geocoder unreachable: {exc.reason}"
        ) from None
    except (OSError, ValueError) as exc:
        raise SourceUnavailableError(
            f"Zillow bounds geocoder bad response: {type(exc).__name__}"
        ) from None

    try:
        south, north, west, east = map(float, body[0]["boundingbox"])
    except (IndexError, KeyError, TypeError, ValueError):
        raise SourceUnavailableError(
            f"no US map bounds found for {city}, {state}"
        ) from None
    if not (south < north and west < east):
        raise SourceUnavailableError(f"invalid map bounds for {city}, {state}")
    return {"west": west, "east": east, "south": south, "north": north}


def resolve_map_bounds(query: SourceQuery, timeout: float = 20.0) -> dict[str, float]:
    """Resolve a US city to the map rectangle required by the Zillow actor.

    OpenStreetMap's Nominatim endpoint is used only for geographic bounds, not
    listing collection.

    Keyed on (city, state) ONLY. A SourceQuery also carries price and bed
    filters, which have no bearing on where a city is; caching on the whole
    query meant several queries over one city each paid for their own geocode.
    Nominatim asks for at most one request per second, and a 429 here surfaces
    as a rate-limit error in the middle of the paid fetch loop.
    """
    return _city_map_bounds(query.city, query.state, min(float(timeout), 20.0))


# Callers (and tests) treat the resolver as the cache boundary; keep that true
# even though the memoized function underneath is now narrower than the query.
resolve_map_bounds.cache_clear = _city_map_bounds.cache_clear
resolve_map_bounds.cache_info = _city_map_bounds.cache_info


@dataclass
class ZillowRentalAdapter:
    """Apify-backed Zillow rental source implementing ``SourceAdapter``."""

    token: str = field(repr=False)
    actor: str = "maxcopell~zillow-scraper"
    results_limit: int = 25
    timeout: float = 300.0
    max_charge_usd: float = 0.25
    days_on_zillow: Optional[int] = None
    bounds_resolver: Callable[[SourceQuery], dict[str, float]] = field(
        default=resolve_map_bounds, repr=False
    )
    client: Optional[ApifyClient] = field(default=None, repr=False)
    poll_seconds: float = 5.0
    sleeper: Callable[[float], Any] = field(default=time.sleep, repr=False)

    name = SOURCE_NAME

    def __post_init__(self) -> None:
        if self.client is None:
            # Poll requests are short; the long wait is the run itself, which
            # this adapter times out on its own terms below.
            self.client = ApifyClient(token=self.token, timeout=min(self.timeout, 60.0))

    def actor_input(self, query: SourceQuery) -> dict[str, Any]:
        map_bounds = self.bounds_resolver(query)
        return {
            "searchUrls": [
                {
                    "url": rental_search_url(
                        query,
                        map_bounds=map_bounds,
                        days_on_zillow=self.days_on_zillow,
                    )
                }
            ],
            "extractionMethod": "PAGINATION_WITH_ZOOM_IN",
            "resultsLimit": self.results_limit,
        }

    def normalize_dataset(self, body: list[Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in body:
            if not isinstance(item, dict) or not self._is_rental(item):
                continue
            records.append(self._canonical(item))
        return records

    def fetch_page(
        self, query: SourceQuery, *, offset: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Start one actor run, wait for it, and map its dataset.

        ``ApifyClient`` already raises the shared taxonomy (auth / rate limit /
        unavailable), so the fetch loop reacts to an Apify failure here exactly
        as it does to any other source.
        """
        if offset != 0:
            return [], False

        state = self.client.start_actor(
            self.actor,
            self.actor_input(query),
            max_total_charge_usd=self.max_charge_usd if self.max_charge_usd > 0 else None,
        )
        waited = 0.0
        while not state.terminal and waited < self.timeout:
            self.sleeper(self.poll_seconds)
            waited += self.poll_seconds
            state = self.client.get_run(state.run_id)

        if not state.terminal:
            # Abandon the WAIT, not the run. It keeps going on Apify and will
            # bill for what it collects either way; re-buying it would just pay
            # twice for the same listings.
            raise SourceUnavailableError(
                f"apify Zillow run {state.run_id} still {state.status} "
                f"after {waited:g}s"
            )
        if not state.succeeded:
            detail = state.status_message or "no detail given"
            raise SourceUnavailableError(
                f"apify Zillow run {state.run_id} {state.status}: {detail}"
            )
        if not state.default_dataset_id:
            raise SourceUnavailableError(
                f"apify Zillow run {state.run_id} succeeded with no dataset"
            )
        return self.normalize_dataset(self.client.get_dataset(state.default_dataset_id)), False

    @staticmethod
    def _is_rental(item: dict[str, Any]) -> bool:
        # The actor represents a valid run with no Zillow hits as one dataset
        # item such as {"error": "No results found."}. Never normalize that
        # control record into an empty listing.
        if item.get("error"):
            return False
        # Building-summary cards contain per-bedroom price RANGES (a ``units``
        # list, e.g. ``{"beds": "1", "price": "$3,100+"}``) instead of one price
        # and bed count for one unit, and no unit-level baths or stable
        # unit-detail URL. They cannot satisfy CHC's exact rental schema
        # without inventing data, so leave them out. The actor has shipped two
        # shapes for this: an older ``isBuilding: true`` flag, and a newer one
        # (seen starting 2026-09-02) with no flag at all, identified instead by
        # a non-empty ``units`` array and a ``/b/`` (not ``/homedetails/``)
        # ``propertyUrl``.
        if item.get("isBuilding") is True:
            return False
        if isinstance(item.get("units"), list) and item["units"]:
            return False
        home = _home_info(item)
        address_info = _address_info(item)
        if not any(
            (
                item.get("zpid"),
                item.get("id"),
                item.get("detailUrl"),
                item.get("propertyUrl"),
                item.get("addressStreet"),
                item.get("address"),
                address_info.get("street"),
                home.get("zpid"),
                home.get("streetAddress"),
            )
        ):
            return False
        raw = item.get("statusType") or item.get("listingStatus") or home.get("homeStatus")
        # The actor sometimes omits status on cards produced by a rental-only
        # URL. Reject only an explicit non-rental value; absence is validated by
        # the URL contract and the canonical required fields downstream.
        # Compared with underscores/spaces stripped so both the older
        # ``FOR_RENT`` shape and the newer camelCase ``forRent`` shape match the
        # same table without needing two copies of it.
        return raw is None or _normalize_status(raw) in _RENTAL_STATUSES_NORMALIZED

    def _canonical(self, item: dict[str, Any]) -> dict[str, Any]:
        home = _home_info(item)
        address_info = _address_info(item)
        price_info = item.get("listingPrice")
        if not isinstance(price_info, dict):
            price_info = {}
        address = (
            item.get("addressStreet")
            or home.get("streetAddress")
            or item.get("address")
            or address_info.get("street")
            or ""
        )
        explicit_unit = item.get("unit") or home.get("unit") or address_info.get("unit")
        address, unit = _address_and_unit(str(address), explicit_unit)
        detail = str(
            item.get("detailUrl") or home.get("hdpUrl") or item.get("propertyUrl") or ""
        )
        if detail.startswith("/"):
            detail = ZILLOW + detail
        home_type = item.get("homeType") or home.get("homeType")
        return {
            "source": self.name,
            "source_listing_id": _text(
                item.get("zpid") or item.get("id") or home.get("zpid")
            ),
            "url": detail,
            "address": address,
            "unit": unit,
            "city": item.get("addressCity") or home.get("city") or address_info.get("city"),
            "state": item.get("addressState") or home.get("state") or address_info.get("state"),
            "postal_code": _text(
                item.get("addressZipcode") or home.get("zipcode") or address_info.get("zipCode")
            ),
            "district": None,
            "price": _whole_number(
                item.get("unformattedPrice") or home.get("price") or price_info.get("amount")
            ),
            "property_type": _PROPERTY_TYPES.get(
                str(home_type or "").upper(), "other"
            ),
            "beds": _beds(
                item.get("beds")
                if item.get("beds") is not None
                else item.get("bedrooms")
                if item.get("bedrooms") is not None
                else home.get("bedrooms")
            ),
            "baths": _number(
                item.get("baths")
                if item.get("baths") is not None
                else item.get("bathrooms")
                if item.get("bathrooms") is not None
                else home.get("bathrooms")
            ),
            "sqft": _whole_number(
                item.get("area") or item.get("livingArea") or home.get("livingArea")
            ),
            "features": [],
            "first_seen_at": item.get("datePostedString") or home.get("datePostedString"),
        }


def _home_info(item: dict[str, Any]) -> dict[str, Any]:
    hdp = item.get("hdpData")
    if not isinstance(hdp, dict):
        return {}
    home = hdp.get("homeInfo")
    return home if isinstance(home, dict) else {}


def _address_info(item: dict[str, Any]) -> dict[str, Any]:
    address = item.get("listingAddress")
    return address if isinstance(address, dict) else {}


def _normalize_status(raw: Any) -> str:
    return str(raw).strip().upper().replace("_", "").replace(" ", "")


def _address_and_unit(address: str, explicit_unit: Any) -> tuple[str, Optional[str]]:
    unit = _text(explicit_unit)
    match = _UNIT_AT_END.search(address)
    if match:
        unit = unit or match.group(1)
        address = address[: match.start()].strip()
    return address.strip(), unit


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _number(value: Any) -> Any:
    """Return an exact numeric value; leave ranges unusable for validation."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace(",", "").replace("$", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    return float(text) if "." in text else int(text)


def _whole_number(value: Any) -> Any:
    number = _number(value)
    if isinstance(number, float):
        return int(number) if number.is_integer() else None
    return number


def _beds(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() == "studio":
        return 0
    return _whole_number(value)


def _parse_retry_after(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (TypeError, ValueError):
        return None
