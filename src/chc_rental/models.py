"""Pydantic models for allowlisted users, preference profiles, and audit events."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator, model_validator

_DELIVERY_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULT_DELIVERY_TIME = "09:00"
DEFAULT_TIMEZONE = "America/New_York"


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


class AllowlistedUser(BaseModel):
    id: Optional[int] = None
    telegram_user_id: int
    display_name: str
    active: bool = True
    created_at: Optional[datetime] = None

    @field_validator("telegram_user_id")
    @classmethod
    def telegram_user_id_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("telegram_user_id must be a positive integer")
        return value


class PreferenceProfileBase(BaseModel):
    profile_name: str
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
    active: bool = True
    delivery_time: str = DEFAULT_DELIVERY_TIME
    timezone: str = DEFAULT_TIMEZONE
    notify_on_no_results: bool = False

    @field_validator("delivery_time")
    @classmethod
    def delivery_time_must_be_strict_24h(cls, value: str) -> str:
        return _validate_delivery_time(value)

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid_iana_zone(cls, value: str) -> str:
        return _validate_timezone(value)

    @field_validator("profile_name", "city")
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
        cleaned = value.strip()
        return cleaned or None

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
    def check_ranges_and_feature_overlap(self) -> "PreferenceProfileBase":
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


class PreferenceProfileCreate(PreferenceProfileBase):
    pass


class PreferenceProfileUpdate(BaseModel):
    profile_name: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    property_types: Optional[list[PropertyType]] = None
    bed_min: Optional[int] = None
    bed_max: Optional[int] = None
    bath_min: Optional[float] = None
    bath_max: Optional[float] = None
    sqft_min: Optional[int] = None
    sqft_max: Optional[int] = None
    required_features: Optional[list[str]] = None
    excluded_features: Optional[list[str]] = None
    daily_cap: Optional[int] = None
    active: Optional[bool] = None
    delivery_time: Optional[str] = None
    timezone: Optional[str] = None
    notify_on_no_results: Optional[bool] = None

    @field_validator("delivery_time")
    @classmethod
    def delivery_time_must_be_strict_24h(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_delivery_time(value)

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid_iana_zone(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_timezone(value)


class PreferenceProfile(PreferenceProfileBase):
    id: int
    telegram_user_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Listing(BaseModel):
    """Canonical rental-listing record used by matching/dedup/scheduling.

    Purely a data shape: nothing here fetches, scrapes, or sends anything.
    """

    source: str
    source_listing_id: Optional[str] = None
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

    @field_validator("source", "address")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned

    @field_validator("source_listing_id", "unit", "district")
    @classmethod
    def normalize_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("property_type", mode="before")
    @classmethod
    def normalize_property_type_string(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
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


class AuditEvent(BaseModel):
    id: Optional[int] = None
    event_type: str
    telegram_user_id: Optional[int] = None
    profile_id: Optional[int] = None
    detail: str = ""
    created_at: Optional[datetime] = None


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class DeliveryRecord(BaseModel):
    """One row per (telegram_user_id, listing_key): the delivery ledger's
    unit of state. `attempt_count`/`max_attempts` bound retries for failed
    sends; a `sent` record is terminal and never retried."""

    id: Optional[int] = None
    telegram_user_id: int
    profile_id: Optional[int] = None
    listing_key: str
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 3
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BudgetState(BaseModel):
    """Global daily source/candidate budget for one calendar `budget_date`.
    `allocated` never exceeds `daily_limit`; the circuit breaker trips once
    it reaches the limit."""

    budget_date: str
    daily_limit: int
    allocated: int = 0
    circuit_breaker_tripped: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.daily_limit - self.allocated)
