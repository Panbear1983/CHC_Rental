"""Coerce raw TUI form input (strings from Input widgets, bools from Switch
widgets) into the existing Pydantic profile models. This module only does type
coercion; business rules (ranges, known property types, feature overlap, ...)
still live in `chc_rental.models` and are enforced by pydantic there.
"""

from __future__ import annotations

from typing import Any, Optional

from chc_rental.models import PreferenceProfileCreate, PreferenceProfileUpdate

REQUIRED_FIELDS = (
    "profile_name",
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
_LIST_FIELDS = ("property_types", "required_features", "excluded_features")


class FormParsingError(ValueError):
    """Raised when a raw form field cannot be coerced to its expected type."""


def _parse_int(raw: str, field: str) -> int:
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise FormParsingError(f"{field} must be a whole number") from exc


def _parse_float(raw: str, field: str) -> float:
    try:
        return float(str(raw).strip())
    except ValueError as exc:
        raise FormParsingError(f"{field} must be a number") from exc


def _parse_csv(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_profile_create_form(data: dict) -> PreferenceProfileCreate:
    missing = [field for field in REQUIRED_FIELDS if _is_blank(data.get(field))]
    if missing:
        raise FormParsingError(f"missing required field(s): {', '.join(missing)}")

    payload: dict[str, Any] = {
        "profile_name": str(data["profile_name"]).strip(),
        "city": str(data["city"]).strip(),
        "district": None if _is_blank(data.get("district")) else str(data["district"]).strip(),
        "required_features": _parse_csv(data.get("required_features")),
        "excluded_features": _parse_csv(data.get("excluded_features")),
        "active": _parse_bool(data.get("active", True)),
        "notify_on_no_results": _parse_bool(data.get("notify_on_no_results", False)),
        "delivery_time": str(data.get("delivery_time") or "09:00").strip(),
        "timezone": str(data.get("timezone") or "America/New_York").strip(),
    }
    for field in _INT_FIELDS:
        payload[field] = _parse_int(data[field], field)
    for field in _OPTIONAL_INT_FIELDS:
        payload[field] = None if _is_blank(data.get(field)) else _parse_int(data[field], field)
    for field in _FLOAT_FIELDS:
        payload[field] = _parse_float(data[field], field)
    payload["property_types"] = _parse_csv(data["property_types"])

    return PreferenceProfileCreate(**payload)


def parse_profile_update_form(data: dict) -> PreferenceProfileUpdate:
    payload: dict[str, Any] = {}

    if "profile_name" in data and not _is_blank(data["profile_name"]):
        payload["profile_name"] = str(data["profile_name"]).strip()
    if "city" in data and not _is_blank(data["city"]):
        payload["city"] = str(data["city"]).strip()
    if "district" in data:
        payload["district"] = None if _is_blank(data["district"]) else str(data["district"]).strip()
    for field in _INT_FIELDS:
        if field in data and not _is_blank(data[field]):
            payload[field] = _parse_int(data[field], field)
    for field in _OPTIONAL_INT_FIELDS:
        if field in data:
            payload[field] = None if _is_blank(data[field]) else _parse_int(data[field], field)
    for field in _FLOAT_FIELDS:
        if field in data and not _is_blank(data[field]):
            payload[field] = _parse_float(data[field], field)
    if "property_types" in data and not _is_blank(data["property_types"]):
        payload["property_types"] = _parse_csv(data["property_types"])
    if "required_features" in data and data["required_features"] is not None:
        payload["required_features"] = _parse_csv(data["required_features"])
    if "excluded_features" in data and data["excluded_features"] is not None:
        payload["excluded_features"] = _parse_csv(data["excluded_features"])
    if "active" in data and data["active"] is not None:
        payload["active"] = _parse_bool(data["active"])
    if "notify_on_no_results" in data and data["notify_on_no_results"] is not None:
        payload["notify_on_no_results"] = _parse_bool(data["notify_on_no_results"])
    for field in ("delivery_time", "timezone"):
        if field in data and not _is_blank(data[field]):
            payload[field] = str(data[field]).strip()

    return PreferenceProfileUpdate(**payload)
