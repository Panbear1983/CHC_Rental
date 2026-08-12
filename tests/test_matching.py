"""Deterministic listing-to-search matching."""

import pytest

from chc_rental.matching import matches_search
from chc_rental.models import Listing

from tests.conftest import make_listing, make_search


def listing(**overrides) -> Listing:
    return Listing.model_validate(make_listing(**overrides))


def test_listing_within_all_bounds_matches():
    assert matches_search(listing(), make_search()) is True


def test_city_must_match_and_is_case_insensitive():
    assert matches_search(listing(city=" AUSTIN "), make_search(city="austin")) is True
    assert matches_search(listing(city="Dallas"), make_search(city="Austin")) is False


def test_district_only_constrains_when_the_search_sets_one():
    search = make_search(district="Downtown")
    assert matches_search(listing(district="Downtown"), search) is True
    assert matches_search(listing(district="Uptown"), search) is False
    assert matches_search(listing(district=None), search) is False
    assert matches_search(listing(district="Anywhere"), make_search(district=None)) is True


def test_missing_district_can_use_an_exact_city_name_fallback():
    assert matches_search(
        listing(city="Brooklyn", state="TX", district=None),
        make_search(city="Brooklyn", state="TX", district="Brooklyn"),
    ) is True
    assert matches_search(
        listing(city="Austin", state="TX", district=None),
        make_search(city="Austin", state="TX", district="Downtown"),
    ) is False


@pytest.mark.parametrize(
    "price,expected", [(999, False), (1000, True), (2000, True), (3000, True), (3001, False)]
)
def test_price_bounds_are_inclusive(price, expected):
    assert matches_search(listing(price=price), make_search()) is expected


def test_property_type_must_be_in_the_search_list():
    search = make_search(property_types=["apartment"])
    assert matches_search(listing(property_type="apartment"), search) is True
    assert matches_search(listing(property_type="house"), search) is False


def test_property_type_accepts_spaced_and_hyphenated_source_spellings():
    search = make_search(property_types=["single_family"])
    assert matches_search(listing(property_type="Single Family"), search) is True
    assert matches_search(listing(property_type="single-family"), search) is True


def test_bed_and_bath_bounds():
    assert matches_search(listing(beds=4), make_search(bed_min=1, bed_max=3)) is False
    assert matches_search(listing(baths=0.5), make_search(bath_min=1.0)) is False


def test_sqft_bounds_apply_only_when_the_listing_states_sqft():
    search = make_search(sqft_min=500, sqft_max=1000)
    assert matches_search(listing(sqft=750), search) is True
    assert matches_search(listing(sqft=499), search) is False
    assert matches_search(listing(sqft=1001), search) is False
    assert matches_search(listing(sqft=None), search) is True


def test_required_and_excluded_features():
    assert matches_search(listing(features=["parking"]), make_search(required_features=["parking"]))
    assert not matches_search(listing(features=[]), make_search(required_features=["parking"]))
    assert not matches_search(
        listing(features=["basement"]), make_search(excluded_features=["basement"])
    )


def test_state_only_constrains_when_the_search_sets_one():
    search = make_search(state="TX")
    assert matches_search(listing(state="TX"), search) is True
    assert matches_search(listing(state="tx"), search) is True
    assert matches_search(listing(state="CA"), search) is False
    assert matches_search(listing(state=None), search) is False, (
        "a stateless listing must not match across state lines"
    )
    assert matches_search(listing(state="CA"), make_search(state=None)) is True


def test_state_accepts_full_name_or_code():
    """Regression for the 2026-08-12 'cannot add search' report: typing the
    full state name (New York) must not be refused when the user means NY."""
    from chc_rental.models import _normalize_state
    assert _normalize_state("New York") == "NY"
    assert _normalize_state("new york") == "NY"
    assert _normalize_state("NY") == "NY"
    assert _normalize_state(" Texas ") == "TX"
    assert _normalize_state("California") == "CA"
    assert _normalize_state("") is None
    for bad in ("Brooklyn", "New Yrok", "ZZZ"):
        try:
            _normalize_state(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been rejected")
