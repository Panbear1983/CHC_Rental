"""Disabled-by-default RentCast rental-listings source boundary.

This module contains no HTTP implementation and no scheduler or delivery path.
A caller must explicitly opt in, set a positive request budget, and inject its
own HTTP transport before a request can be made.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from chc_rental.models import Listing

_RENTCAST_RENTAL_LISTINGS_URL = "https://api.rentcast.io/v1/listings/rental/long-term"
_RENTCAST_TIMEOUT_SECONDS = 10.0


class RentCastSearchQuery(BaseModel):
    """Exactly one supported RentCast rental-listing search selector."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    zip_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    city: str | None = None
    state: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")

    @model_validator(mode="after")
    def exactly_one_selector(self) -> "RentCastSearchQuery":
        has_zip = bool(self.zip_code)
        has_city = bool(self.city)
        if has_zip == has_city:
            raise ValueError("provide exactly one of zip_code or city")
        if has_city and not self.state:
            raise ValueError("a city search requires a two-letter state")
        if has_zip and self.state:
            raise ValueError("state is only allowed with a city search")
        return self


@dataclass(frozen=True)
class RentCastSourceConfig:
    """Explicit, cost-safe source configuration; request budget defaults to zero."""

    api_key: str | None = None
    enabled: bool = False
    max_requests: int = 0

    def __post_init__(self) -> None:
        if self.max_requests < 0:
            raise ValueError("RentCast max_requests must be non-negative")


@dataclass(frozen=True)
class RentCastHttpRequest:
    method: str
    url: str
    headers: dict[str, str]


@dataclass(frozen=True)
class RentCastHttpResponse:
    status_code: int
    json_body: object


@dataclass(frozen=True)
class RentCastFetchResult:
    status: str
    listings: tuple[Listing, ...] = ()


@dataclass(frozen=True)
class RentCastHealth:
    ready: bool
    status: str
    detail: str


def _load_dotenv(path: str | Path) -> dict[str, str]:
    """Read simple local ``KEY=VALUE`` lines without logging their contents."""
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    except OSError as exc:
        raise ValueError("RentCast environment file could not be read") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or not key.strip():
            raise ValueError("RentCast environment file has invalid syntax")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load_rentcast_config(
    *, env_file: str | Path = ".env", environ: Mapping[str, str] | None = None
) -> RentCastSourceConfig:
    """Load only RentCast's three config keys; process environment overrides local file."""
    values = _load_dotenv(env_file)
    values.update(environ if environ is not None else os.environ)
    enabled_text = values.get("RENTCAST_ENABLED", "false").strip().lower()
    if enabled_text not in {"true", "false"}:
        raise ValueError("RENTCAST_ENABLED must be true or false")
    max_requests_text = values.get("RENTCAST_MAX_REQUESTS", "0").strip()
    try:
        max_requests = int(max_requests_text)
    except ValueError as exc:
        raise ValueError("RENTCAST_MAX_REQUESTS must be a non-negative integer") from exc
    return RentCastSourceConfig(
        api_key=values.get("RENTCAST_API_KEY") or None,
        enabled=enabled_text == "true",
        max_requests=max_requests,
    )


def rentcast_health(
    *, env_file: str | Path = ".env", environ: Mapping[str, str] | None = None
) -> RentCastHealth:
    """Safe config-only readiness check; it never constructs or calls a transport."""
    try:
        config = load_rentcast_config(env_file=env_file, environ=environ)
    except ValueError:
        return RentCastHealth(False, "invalid_config", "RentCast configuration is invalid")
    if not config.enabled:
        return RentCastHealth(False, "disabled", "RentCast source is disabled")
    if not config.api_key:
        return RentCastHealth(False, "missing_api_key", "RentCast API key is missing")
    if config.max_requests == 0:
        return RentCastHealth(False, "zero_request_budget", "RentCast request budget is zero")
    return RentCastHealth(True, "ready", "RentCast source is configured")


class RentCastHttpTransport(Protocol):
    """Injected HTTP seam. Implementations must honor keyword-only timeouts."""

    def send(self, request: RentCastHttpRequest, *, timeout: float) -> RentCastHttpResponse: ...


class _RentCastListingRecord(BaseModel):
    """The supported, strictly validated subset of a RentCast listing response."""

    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    listing_id: str = Field(alias="id", min_length=1)
    formatted_address: str = Field(alias="formattedAddress", min_length=1)
    address_line_1: str = Field(alias="addressLine1", min_length=1)
    address_line_2: str | None = Field(default=None, alias="addressLine2")
    city: str = Field(min_length=1)
    county: str | None = None
    price: int
    property_type: str = Field(alias="propertyType", min_length=1)
    bedrooms: int
    bathrooms: float
    square_footage: int | None = Field(default=None, alias="squareFootage")
    features: list[str] = Field(default_factory=list)


def _request_for(query: RentCastSearchQuery, api_key: str) -> RentCastHttpRequest:
    selector = (
        {"zipCode": query.zip_code}
        if query.zip_code
        else {"city": query.city, "state": query.state}
    )
    return RentCastHttpRequest(
        method="GET",
        url=f"{_RENTCAST_RENTAL_LISTINGS_URL}?{urlencode(selector)}",
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )


def _normalize_listings(payload: object) -> tuple[Listing, ...]:
    if not isinstance(payload, list):
        raise ValueError("RentCast response must be a JSON list")
    try:
        records = [_RentCastListingRecord.model_validate(item) for item in payload]
        return tuple(
            Listing.model_validate(
                {
                    "source": "rentcast",
                    "source_listing_id": record.listing_id,
                    "address": record.address_line_1,
                    "unit": record.address_line_2,
                    "city": record.city,
                    "district": record.county,
                    "price": record.price,
                    "property_type": record.property_type,
                    "beds": record.bedrooms,
                    "baths": record.bathrooms,
                    "sqft": record.square_footage,
                    "features": record.features,
                }
            )
            for record in records
        )
    except (ValidationError, ValueError) as exc:
        raise ValueError("RentCast response has an unsupported listing shape") from exc


@dataclass
class RentCastRentalSource:
    """One bounded, injection-only rental-listing fetcher with no default I/O."""

    config: RentCastSourceConfig
    transport: RentCastHttpTransport
    _requests_made: int = field(default=0, init=False, repr=False)

    def fetch(self, query: RentCastSearchQuery) -> RentCastFetchResult:
        if not self.config.enabled or not self.config.api_key:
            return RentCastFetchResult(status="disabled")
        if self.config.max_requests <= self._requests_made:
            return RentCastFetchResult(status="budget_exhausted")

        request = _request_for(query, self.config.api_key)
        self._requests_made += 1
        try:
            response = self.transport.send(request, timeout=_RENTCAST_TIMEOUT_SECONDS)
        except Exception as exc:
            raise ValueError("RentCast request failed") from exc
        if response.status_code != 200:
            raise ValueError("RentCast returned a non-success response")
        try:
            listings = _normalize_listings(response.json_body)
        except ValueError as exc:
            raise ValueError("RentCast returned an invalid response") from exc
        return RentCastFetchResult(status="ok", listings=listings)


def main(argv: Sequence[str] | None = None) -> int:
    """Run a config-only health check; this command cannot fetch listings."""
    parser = argparse.ArgumentParser(description="Safe, config-only RentCast source health check")
    parser.add_argument("--env-file", default=".env", help="local dotenv file (default: .env)")
    args = parser.parse_args(argv)
    health = rentcast_health(env_file=args.env_file)
    print(json.dumps({"ready": health.ready, "status": health.status, "detail": health.detail}, sort_keys=True))
    return 0 if health.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
