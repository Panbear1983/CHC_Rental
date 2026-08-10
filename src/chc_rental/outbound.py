"""Offline outbound-delivery seam for future dedicated-bot integration.

This module deliberately contains no HTTP client, credential loading, polling,
webhook, or scheduler.  It only builds a typed rental-notification body and
provides a transport boundary that is disabled unless a caller later injects
an authorized sender.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from chc_rental.models import Listing


@dataclass(frozen=True)
class RentalNotification:
    """A recipient-free rental payload safe to render in operator output."""

    profile_name: str
    listings: tuple[Listing, ...]

    def render_preview(self) -> str:
        """Render a deterministic body without recipient-routing information."""
        lines = [
            "RENTAL NOTIFICATION",
            f"profile={self.profile_name} listings={len(self.listings)}",
        ]
        for listing in self.listings:
            lines.append(
                f"- {listing.address} | {listing.city} | ${listing.price} | "
                f"{listing.property_type.value} | {listing.beds} bed | {listing.baths:g} bath"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class TransportResult:
    """A local-only delivery outcome; ``preview`` never includes a recipient."""

    delivered: bool
    status: str
    preview: str


class RentalNotificationTransport(Protocol):
    """Typed outbound boundary; implementations receive recipient-free payloads."""

    def send(self, notification: RentalNotification) -> TransportResult: ...


class DisabledTransport:
    """The safe default transport: render only and never perform outbound I/O."""

    def send(self, notification: RentalNotification) -> TransportResult:
        return TransportResult(
            delivered=False,
            status="disabled",
            preview=notification.render_preview(),
        )


class DedicatedBotSender(Protocol):
    """Future @Panbear_Buddy_bot integration point, supplied by its own app."""

    def send_rental_notification(self, *, text: str) -> None: ...


@dataclass(frozen=True)
class DedicatedBotTransport:
    """Opt-in placeholder that cannot send without an injected sender.

    This project neither creates a bot sender nor reads its credentials.  A
    future dedicated-bot process may inject its already-authorized sender.
    """

    enabled: bool = False
    sender: DedicatedBotSender | None = None

    def send(self, notification: RentalNotification) -> TransportResult:
        preview = notification.render_preview()
        if not self.enabled:
            return TransportResult(delivered=False, status="disabled", preview=preview)
        if self.sender is None:
            return TransportResult(delivered=False, status="unavailable", preview=preview)
        self.sender.send_rental_notification(text=preview)
        return TransportResult(delivered=True, status="sent", preview=preview)
