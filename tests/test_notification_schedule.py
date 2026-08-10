"""Pure, deterministic tests for the daily notification scheduling helper.

No Telegram, no daemon, no wall-clock reads: every instant is passed in
explicitly so these tests are fully deterministic.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from chc_rental.models import PreferenceProfile, PropertyType
from chc_rental.notification_schedule import is_profile_due


def make_profile(**overrides) -> PreferenceProfile:
    defaults = dict(
        id=1,
        telegram_user_id=111,
        profile_name="Downtown",
        city="Austin",
        district=None,
        price_min=1000,
        price_max=2000,
        property_types=[PropertyType.APARTMENT],
        bed_min=1,
        bed_max=2,
        bath_min=1.0,
        bath_max=2.0,
        required_features=[],
        excluded_features=[],
        daily_cap=5,
        active=True,
        delivery_time="09:00",
        timezone="America/New_York",
    )
    defaults.update(overrides)
    return PreferenceProfile(**defaults)


def test_not_due_before_local_delivery_time():
    profile = make_profile(delivery_time="09:00", timezone="America/New_York")
    # 08:00 EST == 13:00 UTC in January (no DST).
    now_utc = datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)
    assert is_profile_due(profile, now_utc) is False


def test_due_at_exact_local_delivery_time_when_never_sent():
    profile = make_profile(delivery_time="09:00", timezone="America/New_York")
    # 09:00 EST == 14:00 UTC in January (no DST).
    now_utc = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
    assert is_profile_due(profile, now_utc) is True


def test_due_any_time_after_local_delivery_time_when_never_sent():
    profile = make_profile(delivery_time="09:00", timezone="America/New_York")
    now_utc = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
    assert is_profile_due(profile, now_utc) is True


def test_not_due_again_same_local_calendar_date_after_being_sent():
    profile = make_profile(delivery_time="09:00", timezone="America/New_York")
    last_sent_at_utc = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
    assert is_profile_due(profile, now_utc, last_sent_at_utc=last_sent_at_utc) is False


def test_due_again_on_the_next_local_calendar_date_after_being_sent():
    profile = make_profile(delivery_time="09:00", timezone="America/New_York")
    last_sent_at_utc = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
    assert is_profile_due(profile, now_utc, last_sent_at_utc=last_sent_at_utc) is True


def test_runs_once_per_local_calendar_date_not_utc_date():
    # Tokyo is UTC+9: 2026-01-16 05:00 local is still 2026-01-15 20:00 UTC.
    profile = make_profile(delivery_time="09:00", timezone="Asia/Tokyo")
    last_sent_at_utc = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)  # 10:00 local Jan 15
    now_utc = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)  # 05:00 local Jan 16
    # Local calendar date has advanced to Jan 16, but local time (05:00) is
    # still before delivery_time (09:00), so it is not due yet.
    assert is_profile_due(profile, now_utc, last_sent_at_utc=last_sent_at_utc) is False

    later_now_utc = datetime(2026, 1, 16, 1, 0, tzinfo=timezone.utc)  # 10:00 local Jan 16
    assert is_profile_due(profile, later_now_utc, last_sent_at_utc=last_sent_at_utc) is True


def test_dst_spring_forward_is_handled_via_zoneinfo():
    profile = make_profile(delivery_time="09:00", timezone="America/New_York")

    # Before DST starts (EST, UTC-5): 09:00 local == 14:00 UTC.
    before_dst = datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc)
    assert is_profile_due(profile, before_dst) is True

    # After DST starts (EDT, UTC-4, 2026-03-08): 09:00 local == 13:00 UTC,
    # NOT 14:00 UTC. A naive fixed-offset implementation would get this wrong.
    after_dst_at_old_offset = datetime(2026, 3, 15, 13, 30, tzinfo=timezone.utc)
    assert is_profile_due(profile, after_dst_at_old_offset) is True

    after_dst_before_local_delivery = datetime(2026, 3, 15, 12, 30, tzinfo=timezone.utc)
    assert is_profile_due(profile, after_dst_before_local_delivery) is False


def test_zoneinfo_reference_matches_helper_for_dst_boundary():
    profile = make_profile(delivery_time="09:00", timezone="America/New_York")
    zone = ZoneInfo("America/New_York")
    now_utc = datetime(2026, 3, 15, 13, 0, tzinfo=timezone.utc)
    local = now_utc.astimezone(zone)
    assert local.hour == 9
    assert is_profile_due(profile, now_utc) is True


def test_naive_now_utc_is_rejected():
    profile = make_profile()
    with pytest.raises(ValueError):
        is_profile_due(profile, datetime(2026, 1, 15, 14, 0))


def test_naive_last_sent_at_utc_is_rejected():
    profile = make_profile()
    now_utc = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        is_profile_due(profile, now_utc, last_sent_at_utc=datetime(2026, 1, 15, 14, 0))


def test_helper_is_deterministic_for_same_inputs():
    profile = make_profile()
    now_utc = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
    assert is_profile_due(profile, now_utc) == is_profile_due(profile, now_utc)
