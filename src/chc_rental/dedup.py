"""Stable identity key for canonical listings.

Deliberately conservative: two listings are only ever treated as the same
listing when they share a source listing ID, or, lacking one, an identical
normalized address + unit + source. Nothing here merges listings that merely
look similar.
"""

from __future__ import annotations

from chc_rental.models import Listing


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def dedup_key(listing: Listing) -> str:
    source = _normalize_text(listing.source)
    if listing.source_listing_id:
        return f"id:{source}:{_normalize_text(listing.source_listing_id)}"
    address = _normalize_text(listing.address)
    unit = _normalize_text(listing.unit)
    return f"addr:{source}:{address}:{unit}"
