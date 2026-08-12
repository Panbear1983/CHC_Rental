"""Dedup identity, including direct regressions for the collisions the
2026-08-10 audit found in the SQLite build.
"""

from chc_rental.dedup import dedup_key, upgrade_seen_key
from chc_rental.models import Listing

from tests.conftest import make_listing


def key(**overrides) -> str:
    return dedup_key(Listing.model_validate(make_listing(**overrides)))


def test_same_listing_yields_the_same_key():
    assert key() == key()


def test_case_and_whitespace_are_folded():
    assert key(address="  100   MAIN st ") == key(address="100 Main St")


# --- regressions for the audit's confirmed defects --------------------------


def test_same_address_in_different_cities_does_not_collide():
    """The old key omitted city, so one of these two silently vanished."""
    assert key(city="Austin", price=1500) != key(city="Dallas", price=2400)


def test_same_address_in_different_districts_does_not_collide():
    assert key(district="North Loop") != key(district="South Congress")


def test_key_is_stable_when_the_source_starts_returning_an_id():
    """The old key changed shape with source_listing_id, re-notifying everyone."""
    without_id = key(source_listing_id=None)
    with_id = key(source_listing_id="abc123")
    assert without_id == with_id


def test_separator_cannot_be_forged_by_field_contents():
    """A value containing the separator must not impersonate another listing."""
    assert key(city="Austin", district="x") != key(city="Austin:x", district=None)
    assert key(address="1 Main St", unit="2") != key(address="1 Main St:2", unit=None)


def test_unit_distinguishes_two_listings_at_one_address():
    assert key(unit="4B") != key(unit="5C")


def test_different_sources_are_one_property_identity():
    assert key(source="rentcast") == key(source="zillow")


def test_common_street_suffixes_collapse_across_sources():
    assert key(source="rentcast", address="100 Main Street") == key(
        source="zillow", address="100 Main St."
    )


def test_embedded_and_separate_units_have_one_identity():
    assert key(address="100 Main St Apt 4B", unit=None) == key(
        address="100 Main Street", unit="APT 4B"
    )


def test_v2_seen_key_projects_to_the_new_source_independent_identity():
    old = "v2:rentcast:tx:austin::100%20main%20street:apt%204b"
    assert upgrade_seen_key(old) == key(
        source="zillow", state="TX", city="Austin", address="100 Main St", unit="4B"
    )


def test_same_address_and_city_in_different_states_does_not_collide():
    """v2 regression: "100 Main St, Springfield" exists in a dozen states."""
    assert key(state="IL") != key(state="MO")
