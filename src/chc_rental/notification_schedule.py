"""Pure per-profile local-time due calculation; no transport or scheduler daemon."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def is_profile_due(profile, now_utc: datetime, *, last_sent_at_utc: datetime | None = None) -> bool:
    now = _aware_utc(now_utc, "now_utc")
    zone = ZoneInfo(profile.timezone)
    local_now = now.astimezone(zone)
    hour, minute = map(int, profile.delivery_time.split(":"))
    if (local_now.hour, local_now.minute) < (hour, minute):
        return False
    if last_sent_at_utc is None:
        return True
    last_local = _aware_utc(last_sent_at_utc, "last_sent_at_utc").astimezone(zone)
    return last_local.date() != local_now.date()
