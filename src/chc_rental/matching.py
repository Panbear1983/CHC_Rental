"""Deterministic listing-to-profile matching.

Pure functions only: given a canonical `Listing` and a `PreferenceProfile`,
decide eligibility from typed fields alone. No network, no LLM judgment, no
hidden state — the same inputs always produce the same result.
"""

from __future__ import annotations

from chc_rental.models import Listing, PreferenceProfileBase


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def matches_profile(listing: Listing, profile: PreferenceProfileBase) -> bool:
    if _normalize_text(listing.city) != _normalize_text(profile.city):
        return False

    if profile.district is not None:
        if _normalize_text(listing.district) != _normalize_text(profile.district):
            return False

    if not (profile.price_min <= listing.price <= profile.price_max):
        return False

    if listing.property_type not in profile.property_types:
        return False

    if not (profile.bed_min <= listing.beds <= profile.bed_max):
        return False

    if not (profile.bath_min <= listing.baths <= profile.bath_max):
        return False

    # Square-footage bounds are optional on the profile, and listings often
    # omit sqft entirely; an unknown listing sqft is not held against it.
    if listing.sqft is not None:
        if profile.sqft_min is not None and listing.sqft < profile.sqft_min:
            return False
        if profile.sqft_max is not None and listing.sqft > profile.sqft_max:
            return False

    listing_features = set(listing.features)
    if not set(profile.required_features).issubset(listing_features):
        return False
    if set(profile.excluded_features) & listing_features:
        return False

    return True
