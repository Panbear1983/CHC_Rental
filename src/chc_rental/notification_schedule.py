"""Pure per-profile local-time scheduling; no transport or scheduler daemon."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from chc_rental.models import DeliveryMode


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


def _clock(value: str) -> tuple[int, int]:
    hour, minute = map(int, value.split(":"))
    return hour, minute


def local_day_window_utc(profile, now_utc: datetime) -> tuple[datetime, datetime]:
    """UTC bounds of the recipient's current local calendar day."""
    now = _aware_utc(now_utc, "now_utc")
    zone = ZoneInfo(profile.timezone)
    local_date = now.astimezone(zone).date()
    start = datetime.combine(local_date, time.min, tzinfo=zone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def push_precedes_scrape(profile, settings, *, ref_date) -> bool:
    """True when a person's daily push time is at or before the scrape gate.

    Compares ``profile.delivery_time`` (in the profile's zone) against
    ``settings.scrape_time`` (in the scrape zone), both projected to UTC on
    ``ref_date``. When true, the person is "due" before the day's fetch has run,
    so their first push of the day can be empty. This is a same-calendar-date
    approximation used ONLY to surface a non-blocking operator warning; it never
    blocks a save and small DST/date-wrap imprecision is acceptable.
    """
    scrape_zone = ZoneInfo(settings.scrape_timezone)
    push_zone = ZoneInfo(profile.timezone)
    scrape_h, scrape_m = _clock(settings.scrape_time)
    push_h, push_m = _clock(profile.delivery_time)
    scrape_utc = datetime.combine(
        ref_date, time(scrape_h, scrape_m), tzinfo=scrape_zone
    ).astimezone(timezone.utc)
    push_utc = datetime.combine(
        ref_date, time(push_h, push_m), tzinfo=push_zone
    ).astimezone(timezone.utc)
    return push_utc <= scrape_utc


def notification_not_before(profile, now_utc: datetime) -> datetime:
    """Earliest send time respecting delivery mode and local quiet hours."""
    now = _aware_utc(now_utc, "now_utc")
    zone = ZoneInfo(profile.timezone)
    local_now = now.astimezone(zone)

    if profile.delivery_mode == DeliveryMode.DAILY:
        hour, minute = _clock(profile.delivery_time)
        target = datetime.combine(local_now.date(), time(hour, minute), tzinfo=zone)
        if local_now >= target:
            target = datetime.combine(
                local_now.date() + timedelta(days=1), time(hour, minute), tzinfo=zone
            )
        return target.astimezone(timezone.utc)

    if profile.quiet_hours_start is None or profile.quiet_hours_end is None:
        return now
    start = _clock(profile.quiet_hours_start)
    end = _clock(profile.quiet_hours_end)
    current = (local_now.hour, local_now.minute)
    if start < end:
        if not (start <= current < end):
            return now
        target_date = local_now.date()
    else:
        if current >= start:
            target_date = local_now.date() + timedelta(days=1)
        elif current < end:
            target_date = local_now.date()
        else:
            return now
    target = datetime.combine(target_date, time(*end), tzinfo=zone)
    return target.astimezone(timezone.utc)
