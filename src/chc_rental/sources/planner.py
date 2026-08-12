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

from chc_rental.models import Allowlist
from chc_rental.sources.base import SourceQuery


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
