"""Turn everyone's saved searches into the cheapest set of source queries.

Cost control lives here: two people watching one city must produce ONE fetch,
so queries are deduplicated on (city, state) across all active searches of all
allowlisted people. One planned query is one Apify actor run and therefore one
paid request, so the query COUNT is the cost. A planner that splits a city into
several sub-queries multiplies the monthly bill by that factor — that is exactly
what exhausted the Apify free tier on 2026-08-20 — so any future split must come
with a matching request budget and its own cost test.

Each per-city query carries a FILTER ENVELOPE — the widest price/bed/bath/type
span across every active search in that city. Sources that accept filters
(Zillow via Apify) use it so their bounded result slots hold listings a watcher
could actually want, instead of a generic city dump. The envelope is
deliberately a SUPERSET of every search it covers, so no watcher's match is
filtered out at the source — the precise per-search narrowing still happens
locally in `matching.py`.

A search without a state cannot be planned (US sources require one); it is
skipped with a warning rather than silently, so the operator can see why a
search never produces pushes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from chc_rental.models import Allowlist, Search, property_type_group
from chc_rental.sources.base import SourceQuery

# Groups the Zillow search URL can filter on. "other" has no Zillow toggle, so
# a search of only unmappable types leaves the type filter open and relies on
# local matching. Membership is decided by models.property_type_group — the
# single source of truth both the scrape and matching share. Do not add a second
# property-type map here; that drift is what makes a "house" search fetch
# single_family listings and then reject every one of them.
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
    watches: tuple[QueryWatch, ...]


def _zillow_types(search: Search) -> set[str]:
    """Provider-filterable groups for one search, via the shared type map."""
    return {
        group
        for item in search.property_types
        if (group := property_type_group(item)) in _ZILLOW_FILTERABLE_GROUPS
    }


def _group_by_city(
    allowlist: Allowlist,
) -> tuple[dict[tuple[str, str], tuple[str, list[Search]]], list[str]]:
    """Collect active searches per (city, state), warning on unplannable ones."""
    grouped: dict[tuple[str, str], tuple[str, list[Search]]] = {}
    warnings: list[str] = []
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
    return grouped, warnings


def _envelope_query(city: str, state: str, searches: list[Search]) -> SourceQuery:
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


def plan_envelope_queries(allowlist: Allowlist) -> tuple[list[SourceQuery], list[str]]:
    """One filtered query per watched (city, state). See the module docstring."""
    grouped, warnings = _group_by_city(allowlist)
    queries = [
        _envelope_query(display_city, state, bucket)
        for (_, state), (display_city, bucket) in grouped.items()
    ]
    return queries, warnings


def _zillow_query(search: Search) -> SourceQuery:
    return SourceQuery(
        city=search.city.strip(),
        state=search.state,
        price_min=search.price_min,
        price_max=search.price_max,
        beds_min=search.bed_min,
        beds_max=search.bed_max,
        baths_min=search.bath_min,
        property_types=tuple(sorted(_zillow_types(search))),
    )


def plan_incremental_queries(
    allowlist: Allowlist, *, source: str = "zillow"
) -> tuple[list[PlannedSourceQuery], list[str]]:
    """Plan exact provider envelopes and share only byte-identical scopes.

    The incremental path works per-search rather than per-city envelope, so a
    scope is reused only when two people want provably the same provider query.
    """
    normalized_source = source.strip().lower()
    if normalized_source != "zillow":
        raise ValueError(f"incremental query planning is not implemented for {source!r}")
    grouped: dict[str, tuple[SourceQuery, str, list[QueryWatch]]] = {}
    warnings: list[str] = []
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


def plan_queries(allowlist: Allowlist) -> tuple[list[SourceQuery], list[str]]:
    """The daily path's planner: one filtered actor run per watched city."""
    return plan_envelope_queries(allowlist)
