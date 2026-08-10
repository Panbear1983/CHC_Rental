import pytest
from pydantic import ValidationError

from chc_rental.db import Database
from chc_rental.repositories import AllowlistRepository, AuditRepository, ProfileRepository
from chc_rental.errors import DuplicateProfileNameError, ProfileAccessDeniedError, ProfileNotFoundError
from chc_rental.models import PreferenceProfileCreate, PreferenceProfileUpdate


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def allowlist(db):
    repo = AllowlistRepository(db)
    repo.add_user(111, "User One")
    repo.add_user(222, "User Two")
    return repo


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


def test_allowlisted_user_can_create_a_profile(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    assert created.id is not None
    assert created.telegram_user_id == 111
    assert created.profile_name == "Downtown"
    assert created.active is True


def test_allowlisted_user_can_create_multiple_named_profiles(profiles):
    profiles.create_profile(111, make_profile(profile_name="Downtown"))
    profiles.create_profile(111, make_profile(profile_name="Suburbs", city="Round Rock"))

    listed = profiles.list_profiles(111)
    names = {p.profile_name for p in listed}
    assert names == {"Downtown", "Suburbs"}


def test_duplicate_profile_name_for_same_user_is_rejected(profiles):
    profiles.create_profile(111, make_profile(profile_name="Downtown"))
    with pytest.raises(DuplicateProfileNameError):
        profiles.create_profile(111, make_profile(profile_name="Downtown"))


def test_same_profile_name_allowed_across_different_users(profiles):
    profiles.create_profile(111, make_profile(profile_name="Downtown"))
    other = profiles.create_profile(222, make_profile(profile_name="Downtown", city="Dallas"))
    assert other.telegram_user_id == 222


def test_cross_user_profile_access_is_denied(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    with pytest.raises(ProfileAccessDeniedError):
        profiles.get_profile(222, created.id)


def test_cross_user_profile_access_denial_is_audited(profiles, audit):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    with pytest.raises(ProfileAccessDeniedError):
        profiles.get_profile(222, created.id)

    events = audit.list_events()
    assert any(
        e.event_type == "profile_access_denied" and e.telegram_user_id == 222 and e.profile_id == created.id
        for e in events
    )


def test_owner_can_get_own_profile(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    fetched = profiles.get_profile(111, created.id)
    assert fetched.id == created.id


def test_get_missing_profile_raises_not_found(profiles):
    with pytest.raises(ProfileNotFoundError):
        profiles.get_profile(111, 999)


def test_price_min_must_be_lte_price_max():
    with pytest.raises(ValidationError):
        make_profile(price_min=2000, price_max=1000)


def test_bed_min_must_be_lte_bed_max():
    with pytest.raises(ValidationError):
        make_profile(bed_min=3, bed_max=1)


def test_bath_min_must_be_lte_bath_max():
    with pytest.raises(ValidationError):
        make_profile(bath_min=2.5, bath_max=1.0)


def test_daily_cap_must_be_positive():
    with pytest.raises(ValidationError):
        make_profile(daily_cap=0)


def test_daily_cap_cannot_be_negative():
    with pytest.raises(ValidationError):
        make_profile(daily_cap=-3)


def test_property_types_are_normalized_case_and_whitespace():
    profile = make_profile(property_types=[" Apartment ", "CONDO"])
    assert profile.property_types == ["apartment", "condo"]


def test_property_types_reject_unknown_type():
    with pytest.raises(ValidationError):
        make_profile(property_types=["treehouse"])


def test_property_types_must_be_non_empty():
    with pytest.raises(ValidationError):
        make_profile(property_types=[])


def test_required_features_are_normalized():
    profile = make_profile(required_features=[" Parking ", "IN-UNIT LAUNDRY"])
    assert profile.required_features == ["parking", "in-unit laundry"]


def test_required_and_excluded_features_cannot_overlap():
    with pytest.raises(ValidationError):
        make_profile(required_features=["parking"], excluded_features=["Parking"])


def test_city_cannot_be_blank():
    with pytest.raises(ValidationError):
        make_profile(city="   ")


def test_profile_active_toggle_defaults_true_and_can_be_updated(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    assert created.active is True

    updated = profiles.update_profile(111, created.id, PreferenceProfileUpdate(active=False))
    assert updated.active is False


def test_update_by_non_owner_is_denied(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    with pytest.raises(ProfileAccessDeniedError):
        profiles.update_profile(222, created.id, PreferenceProfileUpdate(active=False))


def test_delete_profile_removes_it_and_records_audit_event(profiles, audit):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    profiles.delete_profile(111, created.id)

    with pytest.raises(ProfileNotFoundError):
        profiles.get_profile(111, created.id)

    events = audit.list_events()
    assert any(e.event_type == "profile_deleted" and e.profile_id == created.id for e in events)


def test_delete_by_non_owner_is_denied(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    with pytest.raises(ProfileAccessDeniedError):
        profiles.delete_profile(222, created.id)


def test_profile_create_is_audited(profiles, audit):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    events = audit.list_events()
    assert any(e.event_type == "profile_created" and e.profile_id == created.id for e in events)


def test_sqft_bounds_default_to_none(profiles):
    created = profiles.create_profile(111, make_profile(profile_name="Downtown"))
    assert created.sqft_min is None
    assert created.sqft_max is None


def test_sqft_bounds_roundtrip_through_create_and_update(profiles):
    created = profiles.create_profile(
        111, make_profile(profile_name="Downtown", sqft_min=500, sqft_max=1200)
    )
    assert (created.sqft_min, created.sqft_max) == (500, 1200)

    updated = profiles.update_profile(
        111, created.id, PreferenceProfileUpdate(sqft_min=None, sqft_max=None)
    )
    assert updated.sqft_min is None
    assert updated.sqft_max is None


def test_single_sqft_bound_is_allowed():
    profile = make_profile(sqft_min=800)
    assert profile.sqft_min == 800
    assert profile.sqft_max is None


def test_sqft_min_must_be_lte_sqft_max():
    with pytest.raises(ValidationError):
        make_profile(sqft_min=1200, sqft_max=500)


def test_sqft_cannot_be_negative():
    with pytest.raises(ValidationError):
        make_profile(sqft_min=-1)
