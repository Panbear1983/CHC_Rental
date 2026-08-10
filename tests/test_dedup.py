from chc_rental.dedup import dedup_key
from chc_rental.models import Listing


def make_listing(**overrides):
    defaults = dict(
        source="rentcast",
        source_listing_id=None,
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


def test_same_source_listing_id_yields_same_key_regardless_of_other_fields():
    a = make_listing(source_listing_id="ABC123", address="123 Main St", price=1500)
    b = make_listing(source_listing_id="ABC123", address="999 Other Ave", price=1800)
    assert dedup_key(a) == dedup_key(b)


def test_source_listing_id_is_normalized_case_and_whitespace_insensitive():
    a = make_listing(source_listing_id="ABC123")
    b = make_listing(source_listing_id=" abc123 ")
    assert dedup_key(a) == dedup_key(b)


def test_different_source_listing_ids_yield_different_keys():
    a = make_listing(source_listing_id="ABC123")
    b = make_listing(source_listing_id="XYZ999")
    assert dedup_key(a) != dedup_key(b)


def test_falls_back_to_address_unit_source_when_no_source_listing_id():
    a = make_listing(source_listing_id=None, address="123 Main St", unit="4B", source="rentcast")
    b = make_listing(source_listing_id=None, address="123 Main St", unit="4B", source="rentcast")
    assert dedup_key(a) == dedup_key(b)


def test_fallback_key_is_normalized_case_and_whitespace_insensitive():
    a = make_listing(source_listing_id=None, address="123 Main St", unit="4B")
    b = make_listing(source_listing_id=None, address=" 123 MAIN st ", unit=" 4b ")
    assert dedup_key(a) == dedup_key(b)


def test_fallback_key_differs_by_unit():
    a = make_listing(source_listing_id=None, address="123 Main St", unit="4B")
    b = make_listing(source_listing_id=None, address="123 Main St", unit="4C")
    assert dedup_key(a) != dedup_key(b)


def test_fallback_key_differs_by_source():
    a = make_listing(source_listing_id=None, address="123 Main St", unit=None, source="rentcast")
    b = make_listing(source_listing_id=None, address="123 Main St", unit=None, source="other")
    assert dedup_key(a) != dedup_key(b)


def test_source_listing_id_key_never_collides_with_fallback_key():
    with_id = make_listing(source_listing_id="123 Main St", address="123 Main St", unit=None)
    without_id = make_listing(source_listing_id=None, address="123 Main St", unit=None)
    assert dedup_key(with_id) != dedup_key(without_id)


def test_dedup_key_is_deterministic_same_input_same_output():
    listing = make_listing(source_listing_id="ABC123")
    assert dedup_key(listing) == dedup_key(listing)
