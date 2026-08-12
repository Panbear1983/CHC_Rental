"""RentCast adapter — the primary licensed US listing source (docs/SOURCES.md).

Endpoint: ``GET /v1/listings/rental/long-term`` with ``X-Api-Key`` auth.
One `fetch_page` call = one HTTP request = one unit of request budget.

Field mapping notes, all consequences of what this endpoint provides:

* No consumer listing URL exists, so the push link is a Google Maps search for
  the address — a deep link, not scraping.
* No amenity/feature list is provided, so ``features`` is always empty and a
  search with ``required_features`` can never match a RentCast listing.
* No district/neighborhood is provided, so ``district`` is always None.
* ``propertyType`` values arrive as "Single Family" etc.; the Listing model's
  normalizer folds them to the enum spelling. A missing type becomes "other"
  rather than rejecting an otherwise-good record.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from chc_rental.envfile import read_env_key
from chc_rental.net import ssl_context
from chc_rental.sources.base import (
    SourceAuthError,
    SourceQuery,
    SourceRateLimitError,
    SourceUnavailableError,
)

RENTCAST_API = "https://api.rentcast.io/v1"
SOURCE_NAME = "rentcast"


def load_rentcast_key(env_path: str = ".env") -> Optional[str]:
    return read_env_key(env_path, "RENTCAST_API_KEY")


def _maps_url(address: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote_plus(address)


@dataclass
class RentCastAdapter:
    """Implements `chc_rental.sources.base.SourceAdapter`."""

    api_key: str = field(repr=False)
    timeout: float = 20.0
    page_size: int = 100  # RentCast allows up to 500; modest pages keep memory and retry cost low.

    name = SOURCE_NAME

    def fetch_page(self, query: SourceQuery, *, offset: int) -> tuple[list[dict[str, Any]], bool]:
        params = {
            "city": query.city,
            "state": query.state,
            "status": "Active",
            "limit": str(self.page_size),
            "offset": str(offset),
        }
        url = f"{RENTCAST_API}/listings/rental/long-term?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"X-Api-Key": self.api_key, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=ssl_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Error strings deliberately name only the method context, never
            # the URL: the URL is clean today, but the habit keeps credentials
            # out of tracebacks if auth ever moves into the query string.
            detail = _error_detail(exc)
            if exc.code in (401, 403):
                # RentCast distinguishes a bad key from an inactive
                # subscription in the body; the operator alert must carry that.
                raise SourceAuthError(
                    f"rentcast rejected the API key (HTTP {exc.code}{detail})"
                ) from None
            if exc.code == 429:
                retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
                raise SourceRateLimitError(
                    "rentcast rate limit (HTTP 429)", retry_after=retry_after
                ) from None
            raise SourceUnavailableError(f"rentcast HTTP {exc.code}{detail}") from None
        except urllib.error.URLError as exc:
            raise SourceUnavailableError(f"rentcast unreachable: {exc.reason}") from None
        except (OSError, ValueError) as exc:
            raise SourceUnavailableError(
                f"rentcast bad response: {type(exc).__name__}"
            ) from None
        if not isinstance(payload, list):
            raise SourceUnavailableError("rentcast returned a non-list payload")
        records = [self._canonical(item) for item in payload if isinstance(item, dict)]
        return records, len(payload) >= self.page_size

    def _canonical(self, item: dict[str, Any]) -> dict[str, Any]:
        """Map one raw RentCast record to the canonical listing dict shape.

        Values are passed through, not validated — `validate_records` decides
        what is usable and quarantines the rest in state/rejected/.
        """
        formatted = item.get("formattedAddress") or ""
        address = item.get("addressLine1") or formatted
        return {
            "source": self.name,
            "source_listing_id": str(item["id"]) if item.get("id") is not None else None,
            "url": _maps_url(formatted or address),
            "address": address,
            "unit": item.get("addressLine2"),
            "city": item.get("city"),
            "state": item.get("state"),
            "postal_code": str(item["zipCode"]) if item.get("zipCode") is not None else None,
            "district": None,
            "price": item.get("price"),
            "property_type": item.get("propertyType") or "other",
            "beds": item.get("bedrooms"),
            "baths": item.get("bathrooms"),
            "sqft": item.get("squareFootage"),
            "features": [],
            "first_seen_at": item.get("listedDate"),
        }


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Extract RentCast's own error message from a failure body, if any."""
    try:
        body = json.loads(exc.read().decode("utf-8"))
        message = body.get("message") or body.get("error")
        return f": {str(message)[:200]}" if message else ""
    except Exception:
        return ""


def _parse_retry_after(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (TypeError, ValueError):
        return None
