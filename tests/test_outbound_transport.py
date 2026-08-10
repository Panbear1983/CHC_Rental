"""Offline outbound-transport seam tests; no real delivery is available."""

from chc_rental.models import Listing


def listing() -> Listing:
    return Listing(
        source="fixture",
        source_listing_id="unit-7",
        address="7 Cedar Lane",
        city="Austin",
        price=1650,
        property_type="apartment",
        beds=2,
        baths=1.0,
    )


def test_rental_notification_renders_safe_deterministic_preview_without_recipient_id():
    from chc_rental.outbound import RentalNotification

    notification = RentalNotification(profile_name="Cedar search", listings=(listing(),))

    assert notification.render_preview() == (
        "RENTAL NOTIFICATION\n"
        "profile=Cedar search listings=1\n"
        "- 7 Cedar Lane | Austin | $1650 | apartment | 2 bed | 1 bath"
    )
    assert "987654321" not in notification.render_preview()


def test_disabled_transport_constructs_and_sends_safe_preview_without_network_io(monkeypatch):
    import socket

    from chc_rental.outbound import DisabledTransport, RentalNotification

    def fail_if_network_is_attempted(*args, **kwargs):
        raise AssertionError("network I/O must not be attempted")

    monkeypatch.setattr(socket, "create_connection", fail_if_network_is_attempted)
    transport = DisabledTransport()
    notification = RentalNotification(profile_name="Cedar search", listings=(listing(),))

    result = transport.send(notification)

    assert result.status == "disabled"
    assert result.delivered is False
    assert result.preview == notification.render_preview()
    assert "987654321" not in result.preview


def test_enabled_dedicated_bot_placeholder_without_injected_sender_cannot_send(monkeypatch):
    import socket

    from chc_rental.outbound import DedicatedBotTransport, RentalNotification

    def fail_if_network_is_attempted(*args, **kwargs):
        raise AssertionError("network I/O must not be attempted")

    monkeypatch.setattr(socket, "create_connection", fail_if_network_is_attempted)
    notification = RentalNotification(profile_name="Cedar search", listings=(listing(),))

    result = DedicatedBotTransport(enabled=True).send(notification)

    assert result.status == "unavailable"
    assert result.delivered is False
    assert result.preview == notification.render_preview()


def test_enabled_dedicated_bot_transport_calls_only_injected_fake_with_deterministic_text():
    from chc_rental.outbound import DedicatedBotTransport, RentalNotification

    class FakeSender:
        def __init__(self):
            self.calls = []

        def send_rental_notification(self, *, text):
            self.calls.append({"text": text})

    sender = FakeSender()
    notification = RentalNotification(profile_name="Cedar search", listings=(listing(),))

    result = DedicatedBotTransport(enabled=True, sender=sender).send(notification)

    expected_text = (
        "RENTAL NOTIFICATION\n"
        "profile=Cedar search listings=1\n"
        "- 7 Cedar Lane | Austin | $1650 | apartment | 2 bed | 1 bath"
    )
    assert result.delivered is True
    assert result.status == "sent"
    assert sender.calls == [{"text": expected_text}]
