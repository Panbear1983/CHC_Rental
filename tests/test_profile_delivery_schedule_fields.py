"""Model + repository coverage for the per-profile delivery_time/timezone fields."""

import pytest
from pydantic import ValidationError

from chc_rental.db import Database
from chc_rental.models import PreferenceProfileCreate, PreferenceProfileUpdate
from chc_rental.repositories import AllowlistRepository, AuditRepository, ProfileRepository


def make_profile(**overrides):
    defaults = dict(
        profile_name="default",
        city="Austin",
        district=None,
        price_min=1000,
        price_max=2000,
        property_types=["apartment"],
        bed_min=1,
        bed_max=2,
        bath_min=1.0,
        bath_max=2.0,
        required_features=[],
        excluded_features=[],
        daily_cap=5,
    )
    defaults.update(overrides)
    return PreferenceProfileCreate(**defaults)


def test_delivery_time_and_timezone_default_when_omitted():
    profile = make_profile()
    assert profile.delivery_time == "09:00"
    assert profile.timezone == "America/New_York"


def test_delivery_time_accepts_strict_24_hour_format():
    profile = make_profile(delivery_time="23:45")
    assert profile.delivery_time == "23:45"


@pytest.mark.parametrize(
    "bad_value",
    ["9:00", "09:5", "24:00", "12:60", "9am", "09-00", " 09:00", "09:00 ", ""],
)
def test_delivery_time_rejects_non_strict_formats(bad_value):
    with pytest.raises(ValidationError):
        make_profile(delivery_time=bad_value)


def test_timezone_accepts_valid_iana_zone():
    profile = make_profile(timezone="Asia/Tokyo")
    assert profile.timezone == "Asia/Tokyo"


@pytest.mark.parametrize("bad_value", ["Not/AZone", "america/new_york!!", "", "PST"])
def test_timezone_rejects_invalid_iana_zone(bad_value):
    with pytest.raises(ValidationError):
        make_profile(timezone=bad_value)


def test_update_model_leaves_delivery_fields_unset_by_default():
    update = PreferenceProfileUpdate()
    assert update.model_dump(exclude_unset=True) == {}


def test_update_model_validates_delivery_time_when_provided():
    with pytest.raises(ValidationError):
        PreferenceProfileUpdate(delivery_time="9:00")


def test_update_model_validates_timezone_when_provided():
    with pytest.raises(ValidationError):
        PreferenceProfileUpdate(timezone="Not/AZone")


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def profiles(db):
    allowlist = AllowlistRepository(db)
    allowlist.add_user(111, "User One")
    audit = AuditRepository(db)
    return ProfileRepository(db, allowlist, audit)


def test_created_profile_persists_default_delivery_fields(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    assert created.delivery_time == "09:00"
    assert created.timezone == "America/New_York"

    fetched = profiles.get_profile(111, created.id)
    assert fetched.delivery_time == "09:00"
    assert fetched.timezone == "America/New_York"


def test_created_profile_persists_custom_delivery_fields(profiles):
    created = profiles.create_profile(
        111,
        make_profile(profile_name="Downtown", delivery_time="18:30", timezone="Europe/London"),
    )
    assert created.delivery_time == "18:30"
    assert created.timezone == "Europe/London"

    fetched = profiles.get_profile(111, created.id)
    assert fetched.delivery_time == "18:30"
    assert fetched.timezone == "Europe/London"


def test_owner_can_update_delivery_time_and_timezone(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    updated = profiles.update_profile(
        111, created.id, PreferenceProfileUpdate(delivery_time="20:15", timezone="Asia/Tokyo")
    )
    assert updated.delivery_time == "20:15"
    assert updated.timezone == "Asia/Tokyo"

    fetched = profiles.get_profile(111, created.id)
    assert fetched.delivery_time == "20:15"
    assert fetched.timezone == "Asia/Tokyo"


def test_update_with_invalid_delivery_time_is_rejected(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    with pytest.raises(ValidationError):
        profiles.update_profile(111, created.id, PreferenceProfileUpdate(delivery_time="9:00"))
