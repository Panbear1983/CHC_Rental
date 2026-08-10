"""Coerce raw TUI input (strings from Input widgets, bools from Switch widgets)
into a `Search`.

This module only does type coercion; every business rule (ranges, known property
types, feature overlap) stays in `chc_rental.models` and is enforced by Pydantic
there.
"""

from __future__ import annotations

from typing import Any, Optional

from chc_rental.models import Search

SEARCH_FIELDS = (
    "name",
    "city",
    "district",
    "price_min",
    "price_max",
    "property_types",
    "bed_min",
    "bed_max",
    "bath_min",
    "bath_max",
    "sqft_min",
    "sqft_max",
    "required_features",
    "excluded_features",
    "daily_cap",
)

REQUIRED_FIELDS = (
    "name",
    "city",
    "price_min",
    "price_max",
    "property_types",
    "bed_min",
    "bed_max",
    "bath_min",
    "bath_max",
    "daily_cap",
)

_INT_FIELDS = ("price_min", "price_max", "bed_min", "bed_max", "daily_cap")
_OPTIONAL_INT_FIELDS = ("sqft_min", "sqft_max")
_FLOAT_FIELDS = ("bath_min", "bath_max")


class FormParsingError(ValueError):
    """Raised when a raw form field cannot be coerced to its expected type."""


def _parse_int(raw: Any, field: str) -> int:
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError) as exc:
        raise FormParsingError(f"{field} must be a whole number") from exc


def _parse_float(raw: Any, field: str) -> float:
    try:
        return float(str(raw).strip())
    except (ValueError, TypeError) as exc:
        raise FormParsingError(f"{field} must be a number") from exc


def _parse_csv(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_search_form(data: dict) -> Search:
    """Build a validated `Search` from raw widget values."""
    missing = [field for field in REQUIRED_FIELDS if _is_blank(data.get(field))]
    if missing:
        raise FormParsingError(f"missing required field(s): {', '.join(missing)}")

    payload: dict[str, Any] = {
        "name": str(data["name"]).strip(),
        "city": str(data["city"]).strip(),
        "district": None if _is_blank(data.get("district")) else str(data["district"]).strip(),
        "property_types": _parse_csv(data["property_types"]),
        "required_features": _parse_csv(data.get("required_features")),
        "excluded_features": _parse_csv(data.get("excluded_features")),
        "active": _parse_bool(data.get("active", True)),
    }
    for field in _INT_FIELDS:
        payload[field] = _parse_int(data[field], field)
    for field in _OPTIONAL_INT_FIELDS:
        payload[field] = None if _is_blank(data.get(field)) else _parse_int(data[field], field)
    for field in _FLOAT_FIELDS:
        payload[field] = _parse_float(data[field], field)

    return Search(**payload)


def search_to_form(search: Search) -> dict[str, Any]:
    """Inverse of `parse_search_form`, for pre-filling the edit modal."""

    def _opt(value: Optional[int]) -> str:
        return "" if value is None else str(value)

    return {
        "name": search.name,
        "city": search.city,
        "district": search.district or "",
        "price_min": str(search.price_min),
        "price_max": str(search.price_max),
        "property_types": ", ".join(pt.value for pt in search.property_types),
        "bed_min": str(search.bed_min),
        "bed_max": str(search.bed_max),
        "bath_min": str(search.bath_min),
        "bath_max": str(search.bath_max),
        "sqft_min": _opt(search.sqft_min),
        "sqft_max": _opt(search.sqft_max),
        "required_features": ", ".join(search.required_features),
        "excluded_features": ", ".join(search.excluded_features),
        "daily_cap": str(search.daily_cap),
        "active": search.active,
    }
