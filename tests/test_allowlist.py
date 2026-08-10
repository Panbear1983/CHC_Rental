import pytest

from chc_rental.db import Database
from chc_rental.repositories import AllowlistRepository, AuditRepository, ProfileRepository
from chc_rental.errors import NotAllowlistedError
from chc_rental.models import PreferenceProfileCreate


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def allowlist(db):
    return AllowlistRepository(db)


@pytest.fixture
def audit(db):
    return AuditRepository(db)


@pytest.fixture
def profiles(db, allowlist, audit):
    return ProfileRepository(db, allowlist, audit)


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


def test_unknown_user_is_not_allowlisted(allowlist):
    assert allowlist.is_allowlisted(999999) is False


def test_owner_can_add_user_to_allowlist(allowlist):
    allowlist.add_user(123456, "Peter")
    assert allowlist.is_allowlisted(123456) is True


def test_adding_same_telegram_id_twice_is_rejected(allowlist):
    allowlist.add_user(123456, "Peter")
    with pytest.raises(ValueError):
        allowlist.add_user(123456, "Peter Again")


def test_unknown_telegram_id_cannot_create_profile(profiles):
    with pytest.raises(NotAllowlistedError):
        profiles.create_profile(999999, make_profile())


def test_unknown_telegram_id_cannot_list_profiles(profiles):
    with pytest.raises(NotAllowlistedError):
        profiles.list_profiles(999999)


def test_denied_access_is_recorded_as_audit_event(profiles, allowlist, audit):
    with pytest.raises(NotAllowlistedError):
        profiles.create_profile(999999, make_profile())

    events = audit.list_events()
    assert any(e.event_type == "profile_access_denied" and e.telegram_user_id == 999999 for e in events)
