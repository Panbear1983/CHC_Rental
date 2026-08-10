"""Typed shapes for the file-based build.

The nesting is person -> one profile -> many searches.  Every rule about ranges,
vocabularies and overlaps lives here so that the store, the pipeline and the TUI
all inherit the same validation from a single place.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_DELIVERY_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULT_DELIVERY_TIME = "09:00"
DEFAULT_TIMEZONE = "America/New_York"
SCHEMA_VERSION = 1


def _validate_delivery_time(value: str) -> str:
    if not _DELIVERY_TIME_RE.match(value):
        raise ValueError("delivery_time must be in strict 24-hour HH:MM format")
    return value


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"timezone must be a valid IANA zone name: {value!r}") from exc
    return value


class PropertyType(str, Enum):
    APARTMENT = "apartment"
    HOUSE = "house"
    CONDO = "condo"
    TOWNHOUSE = "townhouse"
    STUDIO = "studio"
    ROOM = "room"
    SINGLE_FAMILY = "single_family"
    MULTI_FAMILY = "multi_family"


def _normalize_feature_list(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip().lower()
        if not cleaned:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


class Search(BaseModel):
    """One saved search. A profile owns several of these, matched independently."""

    name: str
    active: bool = True
    city: str
    district: Optional[str] = None
    price_min: int
    price_max: int
    property_types: list[PropertyType]
    bed_min: int
    bed_max: int
    bath_min: float
    bath_max: float
    sqft_min: Optional[int] = None
    sqft_max: Optional[int] = None
    required_features: list[str] = []
    excluded_features: list[str] = []
    daily_cap: int

    @field_validator("name", "city")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned

    @field_validator("district")
    @classmethod
    def normalize_district(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("property_types", mode="before")
    @classmethod
    def normalize_property_type_strings(cls, value: list) -> list:
        if not isinstance(value, list):
            return value
        return [item.strip().lower() if isinstance(item, str) else item for item in value]

    @field_validator("property_types")
    @classmethod
    def property_types_must_be_non_empty(cls, value: list[PropertyType]) -> list[PropertyType]:
        if not value:
            raise ValueError("at least one property type is required")
        deduped: list[PropertyType] = []
        seen: set[PropertyType] = set()
        for item in value:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped

    @field_validator("required_features", "excluded_features")
    @classmethod
    def normalize_features(cls, value: list[str]) -> list[str]:
        return _normalize_feature_list(value)

    @field_validator("price_min", "price_max", "bed_min", "bed_max")
    @classmethod
    def must_be_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must not be negative")
        return value

    @field_validator("bath_min", "bath_max")
    @classmethod
    def bath_must_be_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("value must not be negative")
        return value

    @field_validator("sqft_min", "sqft_max")
    @classmethod
    def sqft_must_be_non_negative(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value < 0:
            raise ValueError("value must not be negative")
        return value

    @field_validator("daily_cap")
    @classmethod
    def daily_cap_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("daily_cap must be a positive integer")
        return value

    @model_validator(mode="after")
    def check_ranges_and_feature_overlap(self) -> "Search":
        if self.price_min > self.price_max:
            raise ValueError("price_min must be <= price_max")
        if self.bed_min > self.bed_max:
            raise ValueError("bed_min must be <= bed_max")
        if self.bath_min > self.bath_max:
            raise ValueError("bath_min must be <= bath_max")
        if self.sqft_min is not None and self.sqft_max is not None and self.sqft_min > self.sqft_max:
            raise ValueError("sqft_min must be <= sqft_max")
        overlap = set(self.required_features) & set(self.excluded_features)
        if overlap:
            raise ValueError(f"features cannot be both required and excluded: {sorted(overlap)}")
        return self


class Profile(BaseModel):
    """One person's delivery settings plus every search they have saved."""

    delivery_time: str = DEFAULT_DELIVERY_TIME
    timezone: str = DEFAULT_TIMEZONE
    notify_on_no_results: bool = False
    searches: list[Search] = []

    @field_validator("delivery_time")
    @classmethod
    def delivery_time_must_be_strict_24h(cls, value: str) -> str:
        return _validate_delivery_time(value)

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid_iana_zone(cls, value: str) -> str:
        return _validate_timezone(value)

    @model_validator(mode="after")
    def search_names_must_be_unique(self) -> "Profile":
        names = [search.name.lower() for search in self.searches]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"search names must be unique within a profile: {duplicates}")
        return self

    def active_searches(self) -> list[Search]:
        return [search for search in self.searches if search.active]


class AllowlistEntry(BaseModel):
    """An allowlisted Telegram recipient and their profile."""

    telegram_id: int
    display_name: str
    active: bool = True
    profile: Profile = Profile()

    @field_validator("telegram_id")
    @classmethod
    def telegram_id_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("telegram_id must be a positive integer")
        return value

    @field_validator("display_name")
    @classmethod
    def display_name_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("display_name must not be blank")
        return cleaned


class Allowlist(BaseModel):
    """The whole config file: everyone who may receive a push."""

    schema_version: int = SCHEMA_VERSION
    people: list[AllowlistEntry] = []

    @model_validator(mode="after")
    def telegram_ids_must_be_unique(self) -> "Allowlist":
        ids = [person.telegram_id for person in self.people]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"telegram_id must be unique: {duplicates}")
        return self

    def get(self, telegram_id: int) -> Optional[AllowlistEntry]:
        return next((p for p in self.people if p.telegram_id == telegram_id), None)

    def is_allowlisted(self, telegram_id: int) -> bool:
        """Membership means present AND active. The single source of truth."""
        person = self.get(telegram_id)
        return person is not None and person.active

    def active_people(self) -> list[AllowlistEntry]:
        return [person for person in self.people if person.active]


class Settings(BaseModel):
    """Global run policy. Ceilings here are hard stops, not suggestions."""

    schema_version: int = SCHEMA_VERSION
    scrape_time: str = "08:00"
    scrape_timezone: str = DEFAULT_TIMEZONE
    global_daily_request_budget: int = 100
    per_source_daily_request_budget: int = 50
    live_push_enabled: bool = False
    seen_retention_days: int = 90
    cache_retention_days: int = 7
    rejected_retention_days: int = 30
    backup_retention_days: int = 30

    @field_validator("scrape_time")
    @classmethod
    def scrape_time_must_be_strict_24h(cls, value: str) -> str:
        return _validate_delivery_time(value)

    @field_validator("scrape_timezone")
    @classmethod
    def scrape_timezone_must_be_valid(cls, value: str) -> str:
        return _validate_timezone(value)

    @field_validator(
        "global_daily_request_budget",
        "per_source_daily_request_budget",
        "seen_retention_days",
        "cache_retention_days",
        "rejected_retention_days",
        "backup_retention_days",
    )
    @classmethod
    def must_be_non_negative_int(cls, value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("value must be a whole number")
        if value < 0:
            raise ValueError("value must not be negative")
        return value


class Listing(BaseModel):
    """A canonical rental listing. ``url`` is the payload the push delivers."""

    model_config = ConfigDict(extra="ignore")

    source: str
    source_listing_id: Optional[str] = None
    url: str
    address: str
    unit: Optional[str] = None
    city: str
    district: Optional[str] = None
    price: int
    property_type: PropertyType
    beds: int
    baths: float
    sqft: Optional[int] = None
    features: list[str] = []
    first_seen_at: Optional[datetime] = None

    @field_validator("source", "address", "city")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("url must be an http(s) link")
        return cleaned

    @field_validator("source_listing_id", "unit", "district")
    @classmethod
    def normalize_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("property_type", mode="before")
    @classmethod
    def normalize_property_type_string(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower().replace(" ", "_").replace("-", "_")
        return value

    @field_validator("features")
    @classmethod
    def normalize_features(cls, value: list[str]) -> list[str]:
        return _normalize_feature_list(value)

    @field_validator("price", "beds")
    @classmethod
    def must_be_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must not be negative")
        return value

    @field_validator("baths")
    @classmethod
    def baths_must_be_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("value must not be negative")
        return value

    @field_validator("sqft")
    @classmethod
    def sqft_must_be_non_negative(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value < 0:
            raise ValueError("value must not be negative")
        return value
