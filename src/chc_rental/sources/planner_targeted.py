"""Targeted query planning for CHC_Rental.

This module provides functions to split a SourceQuery envelope into multiple
targeted sub-queries based on price bands and bed groups, increasing the
relevance of fetched results without increasing the total number of requests.
"""

from __future__ import annotations

import hashlib
import json
from typing import List, Tuple

from chc_rental.models import Allowlist, PropertyType, property_type_group, Search
from chc_rental.sources.base import SourceQuery


def _zillow_types(search) -> set[str]:
    """Return the set of Zillow-filterable property type groups for a search."""
    return {
        group
        for item in search.property_types
        if (group := property_type_group(item)) in {
            "apartment",
            "condo",
            "townhouse",
            "single_family",
            "multi_family",
            "manufactured",
            "land",
        }
    }


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
