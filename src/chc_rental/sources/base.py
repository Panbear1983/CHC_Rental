"""Source-adapter contract.

An adapter fetches ONE page of raw records for one (city, state) query and
maps each record into the canonical listing dict shape that
`chc_rental.pipeline.validate_records` validates. Pagination, quota
reservation, caching and retry policy all live in `chc_rental.fetch` — an
adapter performs exactly one metered listing-provider request per
`fetch_page` call, so the budget ledger can meter every paid/source request
identically. An adapter may make a free auxiliary geography lookup needed to
form that provider request; the daily source cache still prevents hourly
repetition.

Error taxonomy (the fetch loop reacts differently to each):

* `SourceAuthError`   — credentials rejected; the whole day's fetch aborts and
  the operator is alerted. Retrying cannot help.
* `SourceRateLimitError` — the source asked us to slow down; carries
  `retry_after` seconds when the source provided one.
* `SourceUnavailableError` — network failure, 5xx, or an unreadable body;
  the current query is abandoned, the others still run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class SourceError(RuntimeError):
    """Base class for adapter failures."""


class SourceAuthError(SourceError):
    """The API key was rejected (401/403)."""


class SourceRateLimitError(SourceError):
    """HTTP 429. ``retry_after`` is seconds to wait, or None if unstated."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SourceUnavailableError(SourceError):
    """Transient failure: network, 5xx, or a body that was not valid JSON."""


@dataclass(frozen=True)
class SourceQuery:
    """One normalized provider query.

    Daily adapters use only city/state. The incremental Zillow planner also
    fills the source-supported filters so bounded actor result slots are not
    consumed by listings every watcher would immediately reject.
    """

    city: str
    state: str
    price_min: int | None = None
    price_max: int | None = None
    beds_min: int | None = None
    beds_max: int | None = None
    baths_min: float | None = None
    property_types: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "state": self.state,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "beds_min": self.beds_min,
            "beds_max": self.beds_max,
            "baths_min": self.baths_min,
            "property_types": list(self.property_types),
        }


class SourceAdapter(Protocol):
    name: str

    def fetch_page(self, query: SourceQuery, *, offset: int) -> tuple[list[dict[str, Any]], bool]:
        """Fetch one page. Returns (canonical record dicts, has_more)."""
        ...
