"""Deterministic listing-to-search matching.

Pure functions only: given a canonical `Listing` and a `Search`, decide
eligibility from typed fields alone. No network, no LLM judgment, no hidden
state — the same inputs always produce the same result.
"""

from __future__ import annotations

from chc_rental.models import Listing, Search


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def matches_search(listing: Listing, search: Search) -> bool:
    if _normalize_text(listing.city) != _normalize_text(search.city):
        return False

    if search.district is not None:
        if _normalize_text(listing.district) != _normalize_text(search.district):
            return False

    if not (search.price_min <= listing.price <= search.price_max):
        return False

    if listing.property_type not in search.property_types:
        return False

    if not (search.bed_min <= listing.beds <= search.bed_max):
        return False

    if not (search.bath_min <= listing.baths <= search.bath_max):
        return False

    # Square-footage bounds are optional on the search, and listings often omit
    # sqft entirely; an unknown listing sqft is not held against it.
    if listing.sqft is not None:
        if search.sqft_min is not None and listing.sqft < search.sqft_min:
            return False
        if search.sqft_max is not None and listing.sqft > search.sqft_max:
            return False

    listing_features = set(listing.features)
    if not set(search.required_features).issubset(listing_features):
        return False
    if set(search.excluded_features) & listing_features:
        return False

    return True
