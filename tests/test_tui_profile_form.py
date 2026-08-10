import pytest
from pydantic import ValidationError

from chc_rental.tui.forms import (
    FormParsingError,
    parse_profile_create_form,
    parse_profile_update_form,
)


def make_form(**overrides):
    defaults = dict(
        profile_name="Downtown",
        city="Austin",
        district="",
        price_min="1000",
        price_max="2000",
        property_types="apartment, condo",
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


def test_valid_form_parses_into_profile_create():
    profile = parse_profile_create_form(make_form())
    assert profile.profile_name == "Downtown"
    assert profile.city == "Austin"
    assert profile.district is None
    assert profile.price_min == 1000
    assert profile.price_max == 2000
    assert profile.property_types == ["apartment", "condo"]
    assert profile.bed_min == 1
    assert profile.bed_max == 2
    assert profile.bath_min == 1.0
    assert profile.bath_max == 2.0
    assert profile.daily_cap == 5
    assert profile.active is True


def test_blank_district_becomes_none():
    profile = parse_profile_create_form(make_form(district="   "))
    assert profile.district is None


def test_district_is_kept_when_provided():
    profile = parse_profile_create_form(make_form(district=" North Loop "))
    assert profile.district == "North Loop"


def test_feature_lists_are_split_on_commas():
    profile = parse_profile_create_form(
        make_form(required_features=" Parking , IN-UNIT LAUNDRY", excluded_features="ground floor")
    )
    assert profile.required_features == ["parking", "in-unit laundry"]
    assert profile.excluded_features == ["ground floor"]


def test_missing_required_field_raises_form_parsing_error():
    form = make_form()
    del form["city"]
    with pytest.raises(FormParsingError):
        parse_profile_create_form(form)


def test_blank_required_field_raises_form_parsing_error():
    with pytest.raises(FormParsingError):
        parse_profile_create_form(make_form(profile_name="   "))


def test_non_numeric_price_raises_form_parsing_error():
    with pytest.raises(FormParsingError):
        parse_profile_create_form(make_form(price_min="not-a-number"))


def test_non_numeric_bath_raises_form_parsing_error():
    with pytest.raises(FormParsingError):
        parse_profile_create_form(make_form(bath_min="lots"))


def test_inverted_price_range_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_profile_create_form(make_form(price_min="2000", price_max="1000"))


def test_inverted_bed_range_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_profile_create_form(make_form(bed_min="3", bed_max="1"))


def test_inverted_bath_range_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_profile_create_form(make_form(bath_min="2.5", bath_max="1.0"))


def test_unknown_property_type_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_profile_create_form(make_form(property_types="treehouse"))


def test_overlapping_required_and_excluded_features_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_profile_create_form(
            make_form(required_features="parking", excluded_features="Parking")
        )


def test_active_toggle_accepts_bool_from_switch_widget():
    profile = parse_profile_create_form(make_form(active=False))
    assert profile.active is False


def test_no_results_toggle_accepts_bool_from_switch_widget():
    profile = parse_profile_create_form(make_form(notify_on_no_results=True))
    assert profile.notify_on_no_results is True


def test_active_toggle_accepts_string_values():
    profile = parse_profile_create_form(make_form(active="no"))
    assert profile.active is False
    profile = parse_profile_create_form(make_form(active="yes"))
    assert profile.active is True


def test_update_form_with_no_fields_produces_empty_update():
    update = parse_profile_update_form({})
    assert update.model_dump(exclude_unset=True) == {}


def test_update_form_only_sets_provided_fields():
    update = parse_profile_update_form({"daily_cap": "9", "active": False})
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"daily_cap": 9, "active": False}


def test_update_form_parses_property_types_and_features():
    update = parse_profile_update_form(
        {"property_types": "studio, room", "required_features": "parking"}
    )
    assert update.property_types == ["studio", "room"]
    assert update.required_features == ["parking"]


def test_update_form_district_can_be_cleared_to_none():
    update = parse_profile_update_form({"district": "   "})
    assert update.district is None


def test_update_form_non_numeric_field_raises_form_parsing_error():
    with pytest.raises(FormParsingError):
        parse_profile_update_form({"price_min": "abc"})


def test_create_form_defaults_delivery_time_and_timezone_when_blank():
    profile = parse_profile_create_form(make_form(delivery_time="", timezone=""))
    assert profile.delivery_time == "09:00"
    assert profile.timezone == "America/New_York"


def test_create_form_accepts_custom_delivery_time_and_timezone():
    profile = parse_profile_create_form(make_form(delivery_time="18:30", timezone="Europe/London"))
    assert profile.delivery_time == "18:30"
    assert profile.timezone == "Europe/London"


def test_create_form_rejects_invalid_delivery_time_format():
    with pytest.raises(ValidationError):
        parse_profile_create_form(make_form(delivery_time="9:00"))


def test_create_form_rejects_invalid_timezone():
    with pytest.raises(ValidationError):
        parse_profile_create_form(make_form(timezone="Not/AZone"))


def test_update_form_only_sets_delivery_time_when_provided():
    update = parse_profile_update_form({"delivery_time": "20:15"})
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"delivery_time": "20:15"}


def test_update_form_only_sets_timezone_when_provided():
    update = parse_profile_update_form({"timezone": "Asia/Tokyo"})
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"timezone": "Asia/Tokyo"}


def test_update_form_blank_delivery_time_is_ignored():
    update = parse_profile_update_form({"delivery_time": ""})
    assert update.model_dump(exclude_unset=True) == {}


def test_blank_or_absent_sqft_bounds_become_none():
    profile = parse_profile_create_form(make_form())
    assert profile.sqft_min is None
    assert profile.sqft_max is None
    profile = parse_profile_create_form(make_form(sqft_min="", sqft_max="   "))
    assert profile.sqft_min is None
    assert profile.sqft_max is None


def test_sqft_bounds_parse_when_provided():
    profile = parse_profile_create_form(make_form(sqft_min=" 500 ", sqft_max="1200"))
    assert profile.sqft_min == 500
    assert profile.sqft_max == 1200


def test_non_numeric_sqft_raises_form_parsing_error():
    with pytest.raises(FormParsingError):
        parse_profile_create_form(make_form(sqft_min="big"))


def test_sqft_min_above_sqft_max_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_profile_create_form(make_form(sqft_min="1200", sqft_max="500"))


def test_update_with_blank_sqft_clears_the_bound():
    updates = parse_profile_update_form({"sqft_min": "", "sqft_max": "  "})
    assert updates.model_dump(exclude_unset=True) == {"sqft_min": None, "sqft_max": None}


def test_update_with_sqft_values_sets_the_bounds():
    updates = parse_profile_update_form({"sqft_min": "600", "sqft_max": "900"})
    assert updates.sqft_min == 600
    assert updates.sqft_max == 900
