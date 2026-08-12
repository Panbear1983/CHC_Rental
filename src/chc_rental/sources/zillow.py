"""Owner-approved, terms-flagged Zillow rental collection through Apify.

This adapter never talks to Zillow directly. It submits one rental search URL
for one ``(city, state)`` query to a pinned managed actor and maps the returned
search cards into CHC_Rental's canonical listing shape. One ``fetch_page`` call
is one actor run and therefore one locally metered request; the actor returns a
bounded batch, so ``has_more`` is always false.

The integration is opt-in in ``Settings.zillow_enabled``. Merely placing an
Apify token in ``.env`` cannot activate it.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Optional

from chc_rental.envfile import read_env_key
from chc_rental.net import ssl_context
from chc_rental.sources.base import (
    SourceAuthError,
    SourceQuery,
    SourceRateLimitError,
    SourceUnavailableError,
)

APIFY_RUN_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
ZILLOW = "https://www.zillow.com"
SOURCE_NAME = "zillow"

_RENTAL_STATUSES = {
    "FOR_RENT",
    "FOR RENT",
    "FOR_RENT_BY_AGENT",
    "FOR_RENT_BY_OWNER",
}

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
    query: SourceQuery, *, map_bounds: Optional[dict[str, float]] = None
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
        price: dict[str, int] = {}
        if query.price_min is not None:
            price["min"] = int(query.price_min)
        if query.price_max is not None:
            price["max"] = int(query.price_max)
        filter_state["price"] = price
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
def resolve_map_bounds(query: SourceQuery, timeout: float = 20.0) -> dict[str, float]:
    """Resolve a US city to the map rectangle required by the Zillow actor.

    OpenStreetMap's Nominatim endpoint is used only for geographic bounds, not
    listing collection. Results are cached in-process, while CHC's daily Zillow
    response cache prevents repeated lookups during the hourly schedule.
    """
    params = urllib.parse.urlencode(
        {
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "us",
            "city": query.city,
            "state": query.state,
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
            f"no US map bounds found for {query.city}, {query.state}"
        ) from None
    if not (south < north and west < east):
        raise SourceUnavailableError(
            f"invalid map bounds for {query.city}, {query.state}"
        )
    return {"west": west, "east": east, "south": south, "north": north}


@dataclass
class ZillowRentalAdapter:
    """Apify-backed Zillow rental source implementing ``SourceAdapter``."""

    token: str = field(repr=False)
    actor: str = "maxcopell~zillow-scraper"
    results_limit: int = 25
    timeout: float = 300.0
    max_charge_usd: float = 0.25
    bounds_resolver: Callable[[SourceQuery], dict[str, float]] = field(
        default=resolve_map_bounds, repr=False
    )

    name = SOURCE_NAME

    def actor_input(self, query: SourceQuery) -> dict[str, Any]:
        map_bounds = self.bounds_resolver(query)
        return {
            "searchUrls": [{"url": rental_search_url(query, map_bounds=map_bounds)}],
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
        if offset != 0:
            return [], False
        payload = self.actor_input(query)
        actor = urllib.parse.quote(self.actor, safe="~")
        params = {"token": self.token}
        if self.max_charge_usd > 0:
            params["maxTotalChargeUsd"] = f"{self.max_charge_usd:g}"
        url = APIFY_RUN_URL.format(actor=actor) + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=ssl_context()
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            if exc.code in (401, 403):
                raise SourceAuthError(
                    f"apify rejected the Zillow source token (HTTP {exc.code}{detail})"
                ) from None
            if exc.code == 429:
                raise SourceRateLimitError(
                    "apify Zillow source rate limit (HTTP 429)",
                    retry_after=_parse_retry_after(exc.headers.get("Retry-After")),
                ) from None
            raise SourceUnavailableError(
                f"apify Zillow source HTTP {exc.code}{detail}"
            ) from None
        except urllib.error.URLError as exc:
            raise SourceUnavailableError(
                f"apify Zillow source unreachable: {exc.reason}"
            ) from None
        except (OSError, ValueError) as exc:
            raise SourceUnavailableError(
                f"apify Zillow source bad response: {type(exc).__name__}"
            ) from None

        if not isinstance(body, list):
            raise SourceUnavailableError("apify Zillow source returned a non-list payload")
        return self.normalize_dataset(body), False

    @staticmethod
    def _is_rental(item: dict[str, Any]) -> bool:
        # The actor represents a valid run with no Zillow hits as one dataset
        # item such as {"error": "No results found."}. Never normalize that
        # control record into an empty listing.
        if item.get("error"):
            return False
        # Building-summary cards contain ranges such as ``1 bed, $3,000+`` but
        # no unit-level baths or stable unit URL. They cannot satisfy CHC's
        # exact rental schema without inventing data, so leave them out.
        if item.get("isBuilding") is True:
            return False
        home = _home_info(item)
        if not any(
            (
                item.get("zpid"),
                item.get("id"),
                item.get("detailUrl"),
                item.get("addressStreet"),
                item.get("address"),
                home.get("zpid"),
                home.get("streetAddress"),
            )
        ):
            return False
        raw = item.get("statusType") or home.get("homeStatus")
        # The actor sometimes omits status on cards produced by a rental-only
        # URL. Reject only an explicit non-rental value; absence is validated by
        # the URL contract and the canonical required fields downstream.
        return raw is None or str(raw).strip().upper() in _RENTAL_STATUSES

    def _canonical(self, item: dict[str, Any]) -> dict[str, Any]:
        home = _home_info(item)
        address = (
            item.get("addressStreet")
            or home.get("streetAddress")
            or item.get("address")
            or ""
        )
        explicit_unit = item.get("unit") or home.get("unit")
        address, unit = _address_and_unit(str(address), explicit_unit)
        detail = str(item.get("detailUrl") or home.get("hdpUrl") or "")
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
            "city": item.get("addressCity") or home.get("city"),
            "state": item.get("addressState") or home.get("state"),
            "postal_code": _text(item.get("addressZipcode") or home.get("zipcode")),
            "district": None,
            "price": _whole_number(item.get("unformattedPrice") or home.get("price")),
            "property_type": _PROPERTY_TYPES.get(
                str(home_type or "").upper(), "other"
            ),
            "beds": _beds(
                item.get("beds") if item.get("beds") is not None else home.get("bedrooms")
            ),
            "baths": _number(
                item.get("baths") if item.get("baths") is not None else home.get("bathrooms")
            ),
            "sqft": _whole_number(item.get("area") or home.get("livingArea")),
            "features": [],
            "first_seen_at": item.get("datePostedString") or home.get("datePostedString"),
        }


def _home_info(item: dict[str, Any]) -> dict[str, Any]:
    hdp = item.get("hdpData")
    if not isinstance(hdp, dict):
        return {}
    home = hdp.get("homeInfo")
    return home if isinstance(home, dict) else {}


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


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8"))
        message = body.get("error") or body.get("message")
        return f": {str(message)[:200]}" if message else ""
    except Exception:
        return ""


def _parse_retry_after(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (TypeError, ValueError):
        return None
