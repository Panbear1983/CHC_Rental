"""Stable per-listing identity used to decide "have we already sent this?".

Two properties matter more than cleverness here:

*   **Location is part of the identity.** The previous key was
    ``addr:{source}:{address}:{unit}``, so "100 Main St" in two different cities
    produced one key and the second subscriber silently never heard about their
    listing.
*   **The shape never changes.** The previous key switched to an id-based form
    whenever the source happened to return an id, so a source that returns ids
    inconsistently gave the same flat two identities and re-notified everyone.
    The key is therefore always derived from the same fields, and
    ``source_listing_id`` is deliberately not one of them.

Known trade-off: two distinct units at one address that both omit a unit number
collapse into a single identity.  That under-notifies rather than spams, which is
the safer failure for a push product.
"""

from __future__ import annotations

from urllib.parse import quote

from chc_rental.models import Listing

_KEY_VERSION = "v1"


def _normalize_text(value: str | None) -> str:
    """Fold case and whitespace. Kept byte-identical to ``matching._normalize_text``."""
    return " ".join((value or "").split()).lower()


def _part(value: str | None) -> str:
    """Percent-escape a component so no value can forge the separator."""
    return quote(_normalize_text(value), safe="")


def dedup_key(listing: Listing) -> str:
    """Return the stable identity for one listing."""
    return ":".join(
        (
            _KEY_VERSION,
            _part(listing.source),
            _part(listing.city),
            _part(listing.district),
            _part(listing.address),
            _part(listing.unit),
        )
    )
