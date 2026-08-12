"""Turn everyone's saved searches into the cheapest set of source queries.

Cost control lives here: two people watching Austin must produce ONE fetch,
so queries are deduplicated on (city, state) across all active searches of
all allowlisted people. Filtering (price, beds, features…) happens locally in
`matching.py` against the fetched pool — the source is only asked "which
active rentals exist in this city".

A search without a state cannot be planned (US sources require one); it is
skipped with a warning rather than silently, so the operator can see why a
search never produces pushes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from chc_rental.models import Allowlist
from chc_rental.sources.base import SourceQuery


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


def plan_queries(allowlist: Allowlist) -> tuple[list[SourceQuery], list[str]]:
    queries: dict[tuple[str, str], SourceQuery] = {}
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
            if key not in queries:
                queries[key] = SourceQuery(city=search.city.strip(), state=search.state)
    return list(queries.values()), warnings


def _zillow_query(search) -> SourceQuery:
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


def plan_incremental_queries(
    allowlist: Allowlist, *, source: str = "zillow"
) -> tuple[list[PlannedSourceQuery], list[str]]:
    """Plan exact provider envelopes and share only byte-identical scopes."""
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
