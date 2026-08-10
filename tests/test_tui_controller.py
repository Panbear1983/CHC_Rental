import pytest
from pydantic import ValidationError

from chc_rental.db import Database
from chc_rental.errors import DuplicateProfileNameError, NotAllowlistedError
from chc_rental.tui.controller import TuiController
from chc_rental.tui.forms import FormParsingError


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def controller(db):
    return TuiController(db)


def make_form(**overrides):
    defaults = dict(
        profile_name="Downtown",
        city="Austin",
        district="",
        price_min="1000",
        price_max="2000",
        property_types="apartment",
        bed_min="1",
        bed_max="2",
        bath_min="1.0",
        bath_max="2.0",
        required_features="",
        excluded_features="",
        daily_cap="5",
        active=True,
    )
    defaults.update(overrides)
    return defaults


def test_list_users_starts_empty(controller):
    assert controller.list_users() == []


def test_add_user_appears_in_list_users(controller):
    controller.add_user(111, "Peter")
    users = controller.list_users()
    assert len(users) == 1
    assert users[0].telegram_user_id == 111
    assert users[0].display_name == "Peter"
    assert users[0].active is True


def test_deactivate_user_flips_active_flag(controller):
    controller.add_user(111, "Peter")
    updated = controller.deactivate_user(111)
    assert updated.active is False

    users = {u.telegram_user_id: u for u in controller.list_users()}
    assert users[111].active is False


def test_deactivate_unknown_user_raises(controller):
    with pytest.raises(ValueError):
        controller.deactivate_user(999999)


def test_deactivated_user_can_no_longer_create_profiles(controller):
    controller.add_user(111, "Peter")
    controller.deactivate_user(111)
    with pytest.raises(NotAllowlistedError):
        controller.create_profile(111, make_form())


def test_create_profile_from_form_data(controller):
    controller.add_user(111, "Peter")
    profile = controller.create_profile(111, make_form())
    assert profile.profile_name == "Downtown"
    assert profile.telegram_user_id == 111


def test_create_profile_for_unknown_user_raises(controller):
    with pytest.raises(NotAllowlistedError):
        controller.create_profile(999999, make_form())


def test_create_profile_with_bad_input_raises_form_parsing_error(controller):
    controller.add_user(111, "Peter")
    with pytest.raises(FormParsingError):
        controller.create_profile(111, make_form(daily_cap="not-a-number"))


def test_create_profile_with_inverted_range_raises_validation_error(controller):
    controller.add_user(111, "Peter")
    with pytest.raises(ValidationError):
        controller.create_profile(111, make_form(price_min="2000", price_max="1000"))


def test_duplicate_profile_name_raises(controller):
    controller.add_user(111, "Peter")
    controller.create_profile(111, make_form())
    with pytest.raises(DuplicateProfileNameError):
        controller.create_profile(111, make_form())


def test_list_profiles_returns_created_profiles(controller):
    controller.add_user(111, "Peter")
    controller.create_profile(111, make_form(profile_name="Downtown"))
    controller.create_profile(111, make_form(profile_name="Suburbs", city="Round Rock"))

    names = {p.profile_name for p in controller.list_profiles(111)}
    assert names == {"Downtown", "Suburbs"}


def test_get_profile_returns_single_profile(controller):
    controller.add_user(111, "Peter")
    created = controller.create_profile(111, make_form())
    fetched = controller.get_profile(111, created.id)
    assert fetched.id == created.id


def test_update_profile_from_form_data(controller):
    controller.add_user(111, "Peter")
    created = controller.create_profile(111, make_form())
    updated = controller.update_profile(111, created.id, {"daily_cap": "9", "active": False})
    assert updated.daily_cap == 9
    assert updated.active is False
    assert updated.profile_name == "Downtown"


def test_delete_profile_removes_it(controller):
    controller.add_user(111, "Peter")
    created = controller.create_profile(111, make_form())
    controller.delete_profile(111, created.id)
    assert controller.list_profiles(111) == []
