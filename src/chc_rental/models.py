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
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_DELIVERY_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULT_DELIVERY_TIME = "09:00"
DEFAULT_TIMEZONE = "America/New_York"
SCHEMA_VERSION = 1
ALERT_CONFIG_SCHEMA_VERSION = 2


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
    # Types RentCast can return that nobody is expected to search for. They
    # exist so a valid source record validates instead of landing in
    # state/rejected/ every single day.
    MANUFACTURED = "manufactured"
    LAND = "land"
    OTHER = "other"


class DeliveryMode(str, Enum):
    """When a profile is eligible for outbound notification planning."""

    DAILY = "daily"
    IMMEDIATE = "immediate"


_US_STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR", "guam": "GU",
    "u.s. virgin islands": "VI", "virgin islands": "VI", "american samoa": "AS",
    "northern mariana islands": "MP",
}


def _normalize_state(value: Optional[str]) -> Optional[str]:
    """Blank -> None; accept a full US state name OR a 2-letter code.

    Full names are mapped to their code so a user who types "New York" is not
    refused when they mean NY. A 2-letter input is accepted as-is (upper-cased):
    kept lenient on purpose so canonical source codes always validate. Any other
    string is rejected with a message that names both accepted forms.
    """
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    upper = cleaned.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper
    code = _US_STATE_NAME_TO_CODE.get(cleaned.lower())
    if code is not None:
        return code
    raise ValueError("state must be a US state name or 2-letter code, e.g. Texas or TX")


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

    # Schema-v1 configs have no stable search identifier. It remains optional
    # until the explicit schema-v2 migration assigns and persists one; legacy
    # daily runs never depend on an ephemeral generated value.
    search_id: Optional[str] = None
    name: str
    active: bool = True
    city: str
    state: Optional[str] = None
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

    @field_validator("search_id")
    @classmethod
    def search_id_must_be_a_uuid(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return str(UUID(cleaned))
        except (ValueError, AttributeError) as exc:
            raise ValueError("search_id must be a UUID") from exc

    @field_validator("district")
    @classmethod
    def normalize_district(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("state")
    @classmethod
    def state_must_be_two_letters(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_state(value)

    @field_validator("property_types", mode="before")
    @classmethod
    def normalize_property_type_strings(cls, value: list) -> list:
        # Keep byte-identical to Listing.normalize_property_type_string, or a
        # search for "single family" can never match a listing of that type.
        if not isinstance(value, list):
            return value
        return [
            item.strip().lower().replace(" ", "_").replace("-", "_")
            if isinstance(item, str)
            else item
            for item in value
        ]

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
    delivery_mode: DeliveryMode = DeliveryMode.DAILY
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
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

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def quiet_hours_must_be_strict_24h(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_delivery_time(value)

    @model_validator(mode="after")
    def search_names_must_be_unique(self) -> "Profile":
        names = [search.name.lower() for search in self.searches]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"search names must be unique within a profile: {duplicates}")
        if (self.quiet_hours_start is None) != (self.quiet_hours_end is None):
            raise ValueError("quiet_hours_start and quiet_hours_end must be set together")
        if (
            self.quiet_hours_start is not None
            and self.quiet_hours_start == self.quiet_hours_end
        ):
            raise ValueError("quiet hours must not cover the entire day")
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
        if self.schema_version >= ALERT_CONFIG_SCHEMA_VERSION:
            search_ids = [
                search.search_id
                for person in self.people
                for search in person.profile.searches
            ]
            if any(search_id is None for search_id in search_ids):
                raise ValueError("schema-v2 searches must have a stable search_id")
            duplicate_search_ids = sorted(
                {
                    search_id
                    for search_id in search_ids
                    if search_id is not None and search_ids.count(search_id) > 1
                }
            )
            if duplicate_search_ids:
                raise ValueError(f"search_id must be unique: {duplicate_search_ids}")
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
    # Optional per-source overrides. Sources absent from this mapping keep the
    # legacy ``per_source_daily_request_budget`` value, so existing config files
    # continue to mean exactly what they meant before multi-source fetching.
    source_daily_request_budgets: dict[str, int] = {
        "zillow": 5,
    }
    # Zillow is an owner-approved, terms-flagged managed source. It must never
    # activate merely because a token happens to exist in the environment.
    zillow_enabled: bool = False
    zillow_actor: str = "maxcopell~zillow-scraper"
    zillow_results_limit: int = 25
    zillow_timeout_seconds: int = 300
    zillow_max_charge_usd: float = 0.25
    live_push_enabled: bool = False
    # The incremental path is additive and inert until this independent global
    # gate is enabled after shadow-mode verification.
    incremental_alerts_enabled: bool = False
    incremental_active_start: str = "08:00"
    incremental_active_end: str = "23:00"
    incremental_scheduler_tick_minutes: int = 15
    incremental_query_lease_minutes: int = 20
    incremental_max_new_starts_per_cycle: int = 1
    incremental_event_retention_days: int = 180
    zillow_incremental_interval_minutes: int = 180
    incremental_canary_telegram_ids: list[int] = []
    incremental_monthly_budget_usd: Optional[float] = None
    # Operator alert channel; None disables alerting entirely.
    owner_telegram_id: Optional[int] = None
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

    @field_validator("incremental_active_start", "incremental_active_end")
    @classmethod
    def incremental_active_hours_must_be_strict_24h(cls, value: str) -> str:
        return _validate_delivery_time(value)

    @field_validator(
        "global_daily_request_budget",
        "per_source_daily_request_budget",
        "seen_retention_days",
        "cache_retention_days",
        "rejected_retention_days",
        "backup_retention_days",
        "incremental_scheduler_tick_minutes",
        "incremental_query_lease_minutes",
        "incremental_max_new_starts_per_cycle",
        "incremental_event_retention_days",
        "zillow_incremental_interval_minutes",
        "zillow_results_limit",
        "zillow_timeout_seconds",
    )
    @classmethod
    def must_be_non_negative_int(cls, value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("value must be a whole number")
        if value < 0:
            raise ValueError("value must not be negative")
        return value

    @model_validator(mode="after")
    def incremental_policy_must_be_coherent(self) -> "Settings":
        if self.incremental_scheduler_tick_minutes <= 0:
            raise ValueError("incremental_scheduler_tick_minutes must be positive")
        if self.incremental_query_lease_minutes <= 0:
            raise ValueError("incremental_query_lease_minutes must be positive")
        if self.incremental_max_new_starts_per_cycle <= 0:
            raise ValueError("incremental_max_new_starts_per_cycle must be positive")
        if self.incremental_event_retention_days <= 0:
            raise ValueError("incremental_event_retention_days must be positive")
        if self.zillow_incremental_interval_minutes <= 0:
            raise ValueError("zillow_incremental_interval_minutes must be positive")
        if self.incremental_active_start == self.incremental_active_end:
            raise ValueError("incremental active window must not cover the entire day")
        return self

    @field_validator("incremental_canary_telegram_ids")
    @classmethod
    def canary_ids_must_be_positive_and_unique(cls, value: list[int]) -> list[int]:
        if any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value):
            raise ValueError("incremental canary Telegram IDs must be positive integers")
        if len(set(value)) != len(value):
            raise ValueError("incremental canary Telegram IDs must be unique")
        return value

    @field_validator("incremental_monthly_budget_usd")
    @classmethod
    def incremental_monthly_budget_must_be_non_negative(
        cls, value: Optional[float]
    ) -> Optional[float]:
        if value is not None and value < 0:
            raise ValueError("incremental_monthly_budget_usd must not be negative")
        return value

    @field_validator("source_daily_request_budgets")
    @classmethod
    def source_budgets_must_be_non_negative(
        cls, value: dict[str, int]
    ) -> dict[str, int]:
        cleaned: dict[str, int] = {}
        for raw_name, raw_limit in value.items():
            name = raw_name.strip().lower()
            if not name:
                raise ValueError("source budget names must not be blank")
            if not isinstance(raw_limit, int) or isinstance(raw_limit, bool) or raw_limit < 0:
                raise ValueError(f"source budget for {name} must be a non-negative whole number")
            cleaned[name] = raw_limit
        return cleaned

    @field_validator("zillow_actor")
    @classmethod
    def zillow_actor_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("zillow_actor must not be blank")
        return cleaned

    @field_validator("zillow_max_charge_usd")
    @classmethod
    def zillow_charge_cap_must_be_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("zillow_max_charge_usd must not be negative")
        return value

    def source_request_budget(self, source: str) -> int:
        """Return a source-specific ceiling with legacy-config fallback."""
        return self.source_daily_request_budgets.get(
            source.strip().lower(), self.per_source_daily_request_budget
        )

    @field_validator("owner_telegram_id")
    @classmethod
    def owner_telegram_id_must_be_positive(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value <= 0:
            raise ValueError("owner_telegram_id must be a positive integer")
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
    state: Optional[str] = None
    postal_code: Optional[str] = None
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

    @field_validator("source_listing_id", "unit", "postal_code", "district")
    @classmethod
    def normalize_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("state")
    @classmethod
    def state_must_be_two_letters(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_state(value)

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
