from chc_rental.matching import matches_profile
from chc_rental.models import Listing, PreferenceProfileCreate


def make_profile(**overrides):
    defaults = dict(
        profile_name="default",
        city="Austin",
        district=None,
        price_min=1000,
        price_max=2000,
        property_types=["apartment", "condo"],
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


def make_listing(**overrides):
    defaults = dict(
        source="rentcast",
        source_listing_id="1",
        address="123 Main St",
        unit=None,
        city="Austin",
        district=None,
        price=1500,
        property_type="apartment",
        beds=2,
        baths=1.0,
        features=[],
    )
    defaults.update(overrides)
    return Listing(**defaults)


def test_matching_listing_within_all_bounds_matches():
    assert matches_profile(make_listing(), make_profile()) is True


def test_city_mismatch_does_not_match():
    assert matches_profile(make_listing(city="Dallas"), make_profile(city="Austin")) is False


def test_city_match_is_case_and_whitespace_insensitive():
    assert matches_profile(make_listing(city=" AUSTIN "), make_profile(city="austin")) is True


def test_district_required_by_profile_must_match():
    profile = make_profile(district="Downtown")
    assert matches_profile(make_listing(district="Downtown"), profile) is True
    assert matches_profile(make_listing(district="Uptown"), profile) is False
    assert matches_profile(make_listing(district=None), profile) is False


def test_profile_without_district_ignores_listing_district():
    profile = make_profile(district=None)
    assert matches_profile(make_listing(district="Anywhere"), profile) is True


def test_price_outside_range_does_not_match():
    profile = make_profile(price_min=1000, price_max=2000)
    assert matches_profile(make_listing(price=999), profile) is False
    assert matches_profile(make_listing(price=2001), profile) is False
    assert matches_profile(make_listing(price=1000), profile) is True
    assert matches_profile(make_listing(price=2000), profile) is True


def test_property_type_must_be_in_profile_list():
    profile = make_profile(property_types=["apartment"])
    assert matches_profile(make_listing(property_type="apartment"), profile) is True
    assert matches_profile(make_listing(property_type="house"), profile) is False


def test_beds_outside_range_does_not_match():
    profile = make_profile(bed_min=1, bed_max=2)
    assert matches_profile(make_listing(beds=0), profile) is False
    assert matches_profile(make_listing(beds=3), profile) is False


def test_baths_outside_range_does_not_match():
    profile = make_profile(bath_min=1.0, bath_max=2.0)
    assert matches_profile(make_listing(baths=0.5), profile) is False
    assert matches_profile(make_listing(baths=2.5), profile) is False


def test_required_feature_missing_from_listing_does_not_match():
    profile = make_profile(required_features=["parking"])
    assert matches_profile(make_listing(features=[]), profile) is False
    assert matches_profile(make_listing(features=["parking"]), profile) is True


def test_required_features_normalized_case_insensitive():
    profile = make_profile(required_features=["Parking"])
    assert matches_profile(make_listing(features=["PARKING"]), profile) is True


def test_excluded_feature_present_on_listing_does_not_match():
    profile = make_profile(excluded_features=["no_pets"])
    assert matches_profile(make_listing(features=["no_pets"]), profile) is False
    assert matches_profile(make_listing(features=[]), profile) is True


def test_same_input_yields_same_output():
    profile = make_profile()
    listing = make_listing()
    assert matches_profile(listing, profile) == matches_profile(listing, profile)


def test_sqft_bounds_filter_when_listing_has_sqft():
    profile = make_profile(sqft_min=500, sqft_max=1000)
    assert matches_profile(make_listing(sqft=750), profile) is True
    assert matches_profile(make_listing(sqft=500), profile) is True
    assert matches_profile(make_listing(sqft=1000), profile) is True
    assert matches_profile(make_listing(sqft=499), profile) is False
    assert matches_profile(make_listing(sqft=1001), profile) is False


def test_sqft_min_only_and_max_only_bounds():
    assert matches_profile(make_listing(sqft=800), make_profile(sqft_min=500)) is True
    assert matches_profile(make_listing(sqft=400), make_profile(sqft_min=500)) is False
    assert matches_profile(make_listing(sqft=400), make_profile(sqft_max=500)) is True
    assert matches_profile(make_listing(sqft=600), make_profile(sqft_max=500)) is False


def test_listing_without_sqft_is_not_rejected_by_sqft_bounds():
    profile = make_profile(sqft_min=500, sqft_max=1000)
    assert matches_profile(make_listing(sqft=None), profile) is True


def test_profile_without_sqft_bounds_ignores_listing_sqft():
    assert matches_profile(make_listing(sqft=10), make_profile()) is True
