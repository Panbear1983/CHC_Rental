"""Offline unit tests for the disabled-by-default RentCast rental adapter."""


def test_missing_or_disabled_config_refuses_without_calling_transport():
    from chc_rental.rentcast import (
        RentCastRentalSource,
        RentCastSearchQuery,
        RentCastSourceConfig,
    )

    class FakeTransport:
        def __init__(self):
            self.calls = []

        def send(self, request, *, timeout):
            self.calls.append((request, timeout))
            raise AssertionError("disabled source must not call transport")

    transport = FakeTransport()
    source = RentCastRentalSource(
        config=RentCastSourceConfig(api_key=None, enabled=False), transport=transport
    )

    result = source.fetch(RentCastSearchQuery(zip_code="78701"))

    assert result.status == "disabled"
    assert result.listings == ()
    assert transport.calls == []


def test_search_query_requires_a_five_digit_zip_or_nonblank_city():
    import pytest

    from chc_rental.rentcast import RentCastSearchQuery

    with pytest.raises(ValueError):
        RentCastSearchQuery(zip_code="not-a-zip")
    assert RentCastSearchQuery(city=" Austin ", state="TX").city == "Austin"


def test_city_search_requires_an_explicit_two_letter_state():
    import pytest

    from chc_rental.rentcast import RentCastSearchQuery

    with pytest.raises(ValueError):
        RentCastSearchQuery(city="Brooklyn")
    assert RentCastSearchQuery(city="Brooklyn", state="NY").state == "NY"


def test_enabled_source_uses_injected_transport_with_bounded_rental_request_and_normalizes_listing():
    from chc_rental.rentcast import (
        RentCastHttpResponse,
        RentCastRentalSource,
        RentCastSearchQuery,
        RentCastSourceConfig,
    )

    class FakeTransport:
        def __init__(self):
            self.calls = []

        def send(self, request, *, timeout):
            self.calls.append((request, timeout))
            return RentCastHttpResponse(
                status_code=200,
                json_body=[
                    {
                        "id": "rc-77",
                        "formattedAddress": " 7 Cedar Lane, Austin, TX 78701 ",
                        "addressLine1": "7 Cedar Lane",
                        "addressLine2": " Apt 4B ",
                        "city": " Austin ",
                        "county": " Travis County ",
                        "price": 1650,
                        "propertyType": "Apartment",
                        "bedrooms": 2,
                        "bathrooms": 1.5,
                        "squareFootage": 850,
                        "features": [" Parking ", "parking", "Laundry"],
                        "lastSeenDate": "2026-08-10T00:00:00.000Z",
                    }
                ],
            )

    transport = FakeTransport()
    result = RentCastRentalSource(
        config=RentCastSourceConfig(api_key="test-key", enabled=True, max_requests=1),
        transport=transport,
    ).fetch(RentCastSearchQuery(zip_code="78701"))

    assert result.status == "ok"
    assert transport.calls[0][0].method == "GET"
    assert transport.calls[0][0].url == "https://api.rentcast.io/v1/listings/rental/long-term?zipCode=78701"
    assert transport.calls[0][0].headers == {"X-Api-Key": "test-key", "Accept": "application/json"}
    assert transport.calls[0][1] == 10.0
    assert result.listings[0].model_dump() == {
        "source": "rentcast",
        "source_listing_id": "rc-77",
        "address": "7 Cedar Lane",
        "unit": "Apt 4B",
        "city": "Austin",
        "district": "Travis County",
        "price": 1650,
        "property_type": "apartment",
        "beds": 2,
        "baths": 1.5,
        "sqft": 850,
        "features": ["parking", "laundry"],
    }


def test_malformed_or_http_error_response_is_rejected_with_sanitized_error():
    import pytest

    from chc_rental.rentcast import (
        RentCastHttpResponse,
        RentCastRentalSource,
        RentCastSearchQuery,
        RentCastSourceConfig,
    )

    class FakeTransport:
        def send(self, request, *, timeout):
            return RentCastHttpResponse(status_code=200, json_body={"error": "bad credentials"})

    source = RentCastRentalSource(
        config=RentCastSourceConfig(api_key="never-disclose-key", enabled=True, max_requests=2),
        transport=FakeTransport(),
    )
    with pytest.raises(ValueError) as error:
        source.fetch(RentCastSearchQuery(city="private query", state="TX"))

    message = str(error.value)
    assert message == "RentCast returned an invalid response"
    assert "never-disclose-key" not in message
    assert "private query" not in message
    assert "X-Api-Key" not in message


def test_listing_records_require_the_strict_json_field_types():
    import pytest

    from chc_rental.rentcast import (
        RentCastHttpResponse,
        RentCastRentalSource,
        RentCastSearchQuery,
        RentCastSourceConfig,
    )

    class FakeTransport:
        def send(self, request, *, timeout):
            return RentCastHttpResponse(
                status_code=200,
                json_body=[
                    {
                        "id": "rc-77",
                        "formattedAddress": "7 Cedar Lane, Austin, TX 78701",
                        "addressLine1": "7 Cedar Lane",
                        "city": "Austin",
                        "price": "1650",
                        "propertyType": "Apartment",
                        "bedrooms": 2,
                        "bathrooms": 1.5,
                    }
                ],
            )

    source = RentCastRentalSource(
        config=RentCastSourceConfig(api_key="test-key", enabled=True, max_requests=1),
        transport=FakeTransport(),
    )
    with pytest.raises(ValueError, match="^RentCast returned an invalid response$"):
        source.fetch(RentCastSearchQuery(city="Austin", state="TX"))


def test_non_success_response_is_sanitized_without_disclosing_request_data():
    import pytest

    from chc_rental.rentcast import (
        RentCastHttpResponse,
        RentCastRentalSource,
        RentCastSearchQuery,
        RentCastSourceConfig,
    )

    class FakeTransport:
        def send(self, request, *, timeout):
            return RentCastHttpResponse(status_code=429, json_body={"message": "rate limited"})

    with pytest.raises(ValueError, match="^RentCast returned a non-success response$") as error:
        RentCastRentalSource(
            config=RentCastSourceConfig(api_key="not-for-output", enabled=True, max_requests=1),
            transport=FakeTransport(),
        ).fetch(RentCastSearchQuery(city="query-not-for-output", state="TX"))

    assert "not-for-output" not in str(error.value)
    assert "query-not-for-output" not in str(error.value)


def test_request_budget_refuses_before_a_second_transport_call():
    from chc_rental.rentcast import (
        RentCastHttpResponse,
        RentCastRentalSource,
        RentCastSearchQuery,
        RentCastSourceConfig,
    )

    class FakeTransport:
        def __init__(self):
            self.calls = 0

        def send(self, request, *, timeout):
            self.calls += 1
            return RentCastHttpResponse(status_code=200, json_body=[])

    transport = FakeTransport()
    source = RentCastRentalSource(
        config=RentCastSourceConfig(api_key="test-key", enabled=True, max_requests=1), transport=transport
    )

    assert source.fetch(RentCastSearchQuery(city="Austin", state="TX")).status == "ok"
    assert source.fetch(RentCastSearchQuery(city="Austin", state="TX")).status == "budget_exhausted"
    assert transport.calls == 1


def test_health_check_loads_only_explicit_env_file_and_refuses_disabled_or_missing_config(tmp_path):
    from chc_rental.rentcast import rentcast_health

    env_file = tmp_path / ".env"
    env_file.write_text("RENTCAST_ENABLED=false\nRENTCAST_MAX_REQUESTS=3\n", encoding="utf-8")

    health = rentcast_health(env_file=env_file, environ={})
    missing_key = rentcast_health(
        env_file=env_file,
        environ={"RENTCAST_ENABLED": "true", "RENTCAST_MAX_REQUESTS": "3"},
    )

    assert health.ready is False
    assert health.status == "disabled"
    assert health.detail == "RentCast source is disabled"
    assert missing_key.ready is False
    assert missing_key.status == "missing_api_key"
    assert missing_key.detail == "RentCast API key is missing"
