"""Stable, source-independent rental identity and seen-key compatibility.

RentCast and Zillow may describe the same rental differently and must not create
two Telegram pushes. Identity therefore uses normalized location + unit, never
the provider name or a provider id. Version-2 keys did include ``source``;
``upgrade_seen_key`` projects them into the new shape at read time so existing
ledgers remain effective without a destructive rewrite.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

from chc_rental.models import Listing

_KEY_VERSION = "v3"

_SUFFIXES = {
    "street": "st",
    "st": "st",
    "avenue": "ave",
    "ave": "ave",
    "av": "ave",
    "road": "rd",
    "rd": "rd",
    "lane": "ln",
    "ln": "ln",
    "drive": "dr",
    "dr": "dr",
    "court": "ct",
    "ct": "ct",
    "place": "pl",
    "pl": "pl",
    "boulevard": "blvd",
    "blvd": "blvd",
    "terrace": "ter",
    "ter": "ter",
    "circle": "cir",
    "cir": "cir",
}

_UNIT_AT_END = re.compile(
    r"\s+(?:(?:apt|apartment|unit|suite|ste)\s+|#)([a-z0-9-]+)\s*$",
    re.IGNORECASE,
)


def _normalize_text(value: str | None) -> str:
    """Fold case and whitespace. Kept byte-identical to ``matching._normalize_text``."""
    return " ".join((value or "").split()).lower()


def _normalize_address_unit(address: str | None, unit: str | None) -> tuple[str, str]:
    raw_address = _normalize_text(address)
    raw_unit = _normalize_text(unit)
    match = _UNIT_AT_END.search(raw_address)
    if match:
        raw_unit = raw_unit or match.group(1)
        raw_address = raw_address[: match.start()].strip()
    raw_address = re.sub(r"[.,]", " ", raw_address)
    tokens = [_SUFFIXES.get(token, token) for token in raw_address.split()]
    normalized_address = " ".join(tokens)
    normalized_unit = re.sub(
        r"^(?:apt|apartment|unit|suite|ste|#)\s*", "", raw_unit
    ).strip()
    return normalized_address, normalized_unit


def _part(value: str | None) -> str:
    """Percent-escape a component so no value can forge the separator."""
    return quote(_normalize_text(value), safe="")


def _key_from_fields(
    *, state: str | None, city: str | None, district: str | None,
    address: str | None, unit: str | None,
) -> str:
    normalized_address, normalized_unit = _normalize_address_unit(address, unit)
    return ":".join(
        (
            _KEY_VERSION,
            _part(state),
            _part(city),
            _part(district),
            _part(normalized_address),
            _part(normalized_unit),
        )
    )


def dedup_key(listing: Listing) -> str:
    """Return the stable identity for one listing."""
    return _key_from_fields(
        state=listing.state,
        city=listing.city,
        district=listing.district,
        address=listing.address,
        unit=listing.unit,
    )


def upgrade_seen_key(key: str) -> str:
    """Map a persisted v2 identity to v3 without rewriting the ledger."""
    if key.startswith(f"{_KEY_VERSION}:") or key.startswith("notice:"):
        return key
    parts = key.split(":")
    # v2:source:state:city:district:address:unit
    if len(parts) == 7 and parts[0] == "v2":
        return _key_from_fields(
            state=unquote(parts[2]),
            city=unquote(parts[3]),
            district=unquote(parts[4]),
            address=unquote(parts[5]),
            unit=unquote(parts[6]),
        )
    return key
