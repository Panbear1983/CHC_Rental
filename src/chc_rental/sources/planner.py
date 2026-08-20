"""Turn everyone's saved searches into the cheapest set of source queries.

This module now supports both the original envelope strategy and a targeted
strategy that splits each envelope into multiple sub-queries (by price band
and bed group) to increase the relevance of fetched results without
increasing the total number of requests.

The targeted strategy is currently hardcoded to be used. In the future, this
will be controlled by a configuration flag.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import List, Tuple

from chc_rental.models import Allowlist, PropertyType, property_type_group, Search
from chc_rental.sources.base import SourceQuery

# Groups the Zillow search URL can filter on. "other" has no Zillow toggle, so
# a search of only unmappable types leaves the type filter open and relies on
# local matching. This mirrors models.property_type_group — the single source
# of truth both the scrape and matching share.
_ZILLOW_FILTERABLE_GROUPS = frozenset(
    {
        "apartment",
        "condo",
        "townhouse",
        "single_family",
        "multi_family",
        "manufactured",
        "land",
    }
)


@dataclass(frozen=True)
class QueryWatch:
    telegram_id: int
    search_id: str
    search_name: str


@dataclass(frozen=True)
class PlannedSourceQuery:
    query_id: str
    source: str
    query: SourceQuery
    query_json: str
    watches: Tuple[QueryWatch, ...]


def _zillow_types(search) -> set[str]:
    return {
        group
        for item in search.property_types
        if (group := property_type_group(item)) in _ZILLOW_FILTERABLE_GROUPS
    }


def _envelope_query(city: str, state: str, searches: List[Search]) -> SourceQuery:
    """Widest filter span covering every search in one (city, state) group.

    A listing wanted by ANY search in the group lies inside this envelope, so
    a single filtered feed feeds every watcher; `matching.py` then applies
    each search's exact bounds. Zillow accepts only a bath MINIMUM, so the
    envelope carries the smallest bath_min and leaves the upper bound to local
    matching.
    """
    return SourceQuery(
        city=city,
        state=state,
        price_min=min(s.price_min for s in searches),
        price_max=max(s.price_max for s in searches),
        beds_min=min(s.bed_min for s in searches),
        beds_max=max(s.bed_max for s in searches),
        baths_min=min(s.bath_min for s in searches),
        property_types=tuple(sorted(set().union(*(_zillow_types(s) for s in searches)))),
    )


def plan_envelope_queries(allowlist: Allowlist) -> Tuple[List[SourceQuery], List[str]]:
    """Original envelope query planning (kept for fallback).

    Turn everyone's saved searches into the cheapest set of source queries.

    Cost control lives here: two people watching one city produce ONE fetch,
    so queries are deduplicated on (city, state) across all active searches of
    all allowlisted people.

    Each per-city query carries a FILTER ENVELOPE — the widest price/bed/bath/type
    span across every active search in that city. Sources that accept filters
    (Zillow via Apify) use it so their bounded result slots hold listings a
    watcher could actually want, instead of a generic city dump; sources that do
    not (RentCast) simply ignore the extra fields. The envelope is deliberately a
    SUPERSET of every search it covers, so no watcher's match is fetched out — the
    precise per-search narrowing still happens locally in `matching.py`.

    A search without a state cannot be planned (US states require one); it is
    skipped with a warning rather than silently, so the operator can see why a
    search never produces pushes.
    """
    grouped: dict[Tuple[str, str], Tuple[str, List[Search]]] = {}
    warnings: List[str] = []
    for person in allowlist.active_people():
        for search in person.profile.active_searches():
            if not search.state:
                warnings.append(
                    f"search {search.name!r} of {person.display_name} "
                    f"({person.telegram_id}) has no state; cannot fetch for it"
                )
                continue
            key = (search.city.strip().lower(), search.state)
            display_city, bucket = grouped.setdefault(key, (search.city.strip(), []))
            bucket.append(search)
    queries = [
        _envelope_query(display_city, state, bucket)
        for (_, state), (display_city, bucket) in grouped.items()
    ]
    return queries, warnings


def _split_envelope(envelope: SourceQuery, searches: List[Search]) -> List[SourceQuery]:
    """Split one envelope into multiple targeted sub-queries.

    Args:
        envelope: The original envelope query (superset of all searches in a city).
        searches: The list of searches that the envelope covers.

    Returns:
        A list of targeted SourceQuery objects.
    """
    # If there are no searches, return the original envelope (should not happen in practice)
    if not searches:
        return [envelope]

    # Split the price range into 3 equal bands
    price_min = envelope.price_min
    price_max = envelope.price_max
    price_range = price_max - price_min
    if price_range <= 0:
        # Avoid division by zero; if the range is zero or negative, use the original min and max
        price_bands = [(price_min, price_max)]
    else:
        band_width = price_range / 3.0
        price_bands = [
            (price_min, price_min + band_width),
            (price_min + band_width, price_min + 2 * band_width),
            (price_min + 2 * band_width, price_max),
        ]

    # Split the bed range into 2 groups (low and high)
    beds_min = envelope.beds_min
    beds_max = envelope.beds_max
    beds_range = beds_max - beds_min
    if beds_range <= 0:
        bed_groups = [(beds_min, beds_max)]
    else:
        # We'll split into two groups: low and high
        mid = (beds_min + beds_max) / 2.0
        bed_groups = [
            (beds_min, mid),
            (mid, beds_max),
        ]

    # Generate the Cartesian product of price bands and bed groups
    queries: List[SourceQuery] = []
    for pmin, pmax in price_bands:
        for bmin, bmax in bed_groups:
            # Ensure we don't create invalid queries (e.g., min > max)
            if pmin > pmax or bmin > bmax:
                continue
            queries.append(SourceQuery(
                city=envelope.city,
                state=envelope.state,
                price_min=int(pmin),
                price_max=int(pmax),
                beds_min=int(bmin),
                beds_max=int(bmax),
                baths_min=envelope.baths_min,
                property_types=envelope.property_types,
            ))

    return queries


def plan_targeted_queries(allowlist: Allowlist) -> Tuple[List[SourceQuery], List[str]]:
    """Plan targeted queries for the given allowlist.

    This function splits the envelope query for each (city, state) group into
    multiple targeted sub-queries (by price band and bed group) to increase
    the relevance of the fetched results.

    Args:
        allowlist: The Allowlist object containing all active searches.

    Returns:
        A tuple of (list of planned queries, list of warnings).
    """
    # Group searches by (city, state) and collect warnings for missing state
    grouped: dict[Tuple[str, str], Tuple[str, List[Search]]] = {}
    warnings: List[str] = []
    for person in allowlist.active_people():
        for search in person.profile.active_searches():
            if not search.state:
                warnings.append(
                    f"search {search.name!r} of {person.display_name} "
                    f"({person.telegram_id}) has no state; cannot fetch for it"
                )
                continue
            key = (search.city.strip().lower(), search.state)
            display_city, bucket = grouped.setdefault(key, (search.city.strip(), []))
            bucket.append(search)

    # For each group, compute the envelope and then split it into targeted queries
    targeted_queries: List[SourceQuery] = []
    for (_, state), (display_city, searches) in grouped.items():
        # Compute the envelope for this group (same logic as in plan_envelope_queries)
        envelope = SourceQuery(
            city=display_city,
            state=state,
            price_min=min(s.price_min for s in searches),
            price_max=max(s.price_max for s in searches),
            beds_min=min(s.bed_min for s in searches),
            beds_max=max(s.bed_max for s in searches),
            baths_min=min(s.bath_min for s in searches),
            property_types=tuple(sorted(set().union(*(_zillow_types(s) for s in searches)))),
        )
        # Split the envelope using the actual searches in this group
        queries = _split_envelope(envelope, searches)
        targeted_queries.extend(queries)

    # If we didn't generate any queries (should not happen), return empty list
    if not targeted_queries:
        targeted_queries = []

    return targeted_queries, warnings


def _zillow_query(search: Search) -> SourceQuery:
    provider_types = tuple(
        sorted(
            {
                mapped
                for item in search.property_types
                if (mapped := _ZILLOW_TYPE_GROUPS.get(item.value)) is not None
            }
        )
    )
    return SourceQuery(
        city=search.city.strip(),
        state=search.state,
        price_min=search.price_min,
        price_max=search.price_max,
        beds_min=search.bed_min,
        beds_max=search.bed_max,
        baths_min=search.bath_min,
        property_types=provider_types,
    )


# Mapping from Zillow property types to our canonical groups (used in _zillow_query)
_ZILLOW_TYPE_GROUPS = {
    "apartment": "apartment",
    "studio": "apartment",
    "room": "apartment",
    "condo": "condo",
    "townhouse": "townhouse",
    "house": "single_family",
    "single_family": "single_family",
    "multi_family": "multi_family",
    "manufactured": "manufactured",
    "land": "land",
}


def plan_incremental_queries(
    allowlist: Allowlist, *, source: str = "zillow"
) -> Tuple[List[PlannedSourceQuery], List[str]]:
    """Plan exact provider envelopes and share only byte-identical scopes.

    This function is used by the incremental path and is NOT affected by the
    targeted/query strategy changes. It works per-search, not per-envelope.
    """
    normalized_source = source.strip().lower()
    if normalized_source != "zillow":
        raise ValueError(f"incremental query planning is not implemented for {source!r}")
    grouped: dict[str, tuple[SourceQuery, str, list[QueryWatch]]] = {}
    warnings: List[str] = []
    for person in allowlist.active_people():
        for search in person.profile.active_searches():
            if not search.state:
                warnings.append(
                    f"search {search.name!r} of {person.display_name} "
                    f"({person.telegram_id}) has no state; cannot fetch for it"
                )
                continue
            if not search.search_id:
                warnings.append(
                    f"search {search.name!r} of {person.display_name} "
                    "has no stable search_id; run alerts migrate --apply"
                )
                continue
            query = _zillow_query(search)
            canonical = json.dumps(
                {"source": normalized_source, "query": query.as_dict()},
                sort_keys=True,
                separators=(",", ":"),
            )
            query_id = f"{normalized_source}:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"
            if query_id not in grouped:
                grouped[query_id] = (query, canonical, [])
            grouped[query_id][2].append(
                QueryWatch(
                    telegram_id=person.telegram_id,
                    search_id=search.search_id,
                    search_name=search.name,
                )
            )
    planned = [
        PlannedSourceQuery(
            query_id=query_id,
            source=normalized_source,
            query=query,
            query_json=canonical,
            watches=tuple(sorted(watches, key=lambda item: (item.telegram_id, item.search_id))),
        )
        for query_id, (query, canonical, watches) in sorted(grouped.items())
    ]
    return planned, warnings


def plan_queries(allowlist: Allowlist) -> Tuple[List[SourceQuery], List[str]]:
    """Plan queries for the given allowlist.

    This function currently uses the targeted strategy. In the future, this
    will be controlled by a configuration flag.

    Args:
        allowlist: The Allowlist object containing all active searches.

    Returns:
        A tuple of (list of planned queries, list of warnings).
    """
    # For now, we use the targeted strategy. To switch back to the envelope
    # strategy, we would change this to `return plan_envelope_queries(allowlist)`.
    return plan_targeted_queries(allowlist)