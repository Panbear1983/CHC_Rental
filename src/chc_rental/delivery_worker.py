"""Durable, canary-only Telegram delivery for incremental alert outbox rows."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from chc_rental.dedup import upgrade_seen_key
from chc_rental.event_store import OutboxRecord
from chc_rental.matching import matches_search
from chc_rental.models import DeliveryMode, Listing, Settings
from chc_rental.notify.telegram import TelegramReceipt, TelegramSendError
from chc_rental.notification_schedule import notification_not_before
from chc_rental.store import Store


class IncrementalSender(Protocol):
    def send(self, *, telegram_id: int, text: str) -> Any: ...


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    permanent: bool
    reason: str


@dataclass
class DeliveryReport:
    promoted: int = 0
    sent: int = 0
    retry_wait: int = 0
    failed: int = 0
    uncertain: int = 0
    cancelled: int = 0
    reconciled_seen: int = 0
    stale_sending_recovered: int = 0
    blocked: list[str] = field(default_factory=list)
    sent_outbox_ids: list[int] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "promoted": self.promoted,
            "sent": self.sent,
            "retry_wait": self.retry_wait,
            "failed": self.failed,
            "uncertain": self.uncertain,
            "cancelled": self.cancelled,
            "reconciled_seen": self.reconciled_seen,
            "stale_sending_recovered": self.stale_sending_recovered,
            "blocked": self.blocked,
            "sent_outbox_ids": self.sent_outbox_ids,
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("delivery worker timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _receipt_message_id(result: Any) -> str | None:
    if isinstance(result, TelegramReceipt):
        return result.message_id
    if isinstance(result, dict) and result.get("message_id") is not None:
        return str(result["message_id"])
    return None


class DeliveryWorker:
    def __init__(
        self,
        store: Store,
        *,
        sender: IncrementalSender,
        max_attempts: int = 4,
        retry_base_seconds: int = 60,
        sending_stale_minutes: int = 10,
    ) -> None:
        self.store = store
        self.events = store.event_store()
        self.sender = sender
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.sending_stale_minutes = sending_stale_minutes

    @staticmethod
    def _global_gate(settings: Settings) -> str | None:
        if not settings.live_push_enabled:
            return "live_push_enabled is off"
        if not settings.incremental_alerts_enabled:
            return "incremental_alerts_enabled is off"
        if not settings.zillow_enabled:
            return "Zillow source is off"
        return None

    def _targets(
        self, settings: Settings, requested_telegram_ids: set[int] | None
    ) -> set[int]:
        canaries = set(settings.incremental_canary_telegram_ids)
        if requested_telegram_ids is None:
            return canaries
        outside = requested_telegram_ids - canaries
        if outside:
            raise ValueError(
                "requested Telegram IDs are not configured canaries: "
                + ", ".join(map(str, sorted(outside)))
            )
        return set(requested_telegram_ids)

    def _eligibility(self, row: OutboxRecord, settings: Settings) -> Eligibility:
        gate = self._global_gate(settings)
        if gate:
            return Eligibility(False, False, gate)
        if row.telegram_id not in settings.incremental_canary_telegram_ids:
            return Eligibility(False, False, "recipient is not an incremental canary")
        allowlist = self.store.load_allowlist()
        person = allowlist.get(row.telegram_id)
        if person is None or not allowlist.is_allowlisted(row.telegram_id):
            return Eligibility(False, True, "recipient is no longer allowlisted")
        if person.profile.delivery_mode != DeliveryMode.IMMEDIATE:
            return Eligibility(False, False, "recipient delivery mode is not immediate")
        try:
            listing = Listing.model_validate_json(
                self.events.outbox_listing_json(row.outbox_id)
            )
        except Exception as exc:
            return Eligibility(False, True, f"listing context is invalid: {type(exc).__name__}")
        matching_ids = set(row.matching_search_ids)
        searches = [
            search
            for search in person.profile.active_searches()
            if search.search_id in matching_ids
        ]
        if not searches or not any(matches_search(listing, search) for search in searches):
            return Eligibility(False, True, "no current active search still matches")
        seen = {upgrade_seen_key(key) for key in self.store.seen_keys(row.telegram_id)}
        if row.identity_key in seen:
            return Eligibility(False, True, "listing was already sent by the daily path")
        return Eligibility(True, False, "eligible")

    def _reconcile_seen(self, targets: set[int], now: datetime) -> int:
        reconciled = 0
        for row in self.events.outbox_records(status="sent"):
            if row.telegram_id not in targets:
                continue
            seen = {
                upgrade_seen_key(key) for key in self.store.seen_keys(row.telegram_id)
            }
            if row.identity_key in seen:
                continue
            try:
                listing = Listing.model_validate_json(
                    self.events.outbox_listing_json(row.outbox_id)
                )
                self.store.mark_seen(
                    row.telegram_id,
                    row.identity_key,
                    search_name=row.primary_search_name,
                    url=listing.url,
                    now_utc=now,
                )
            except Exception:
                continue
            reconciled += 1
        return reconciled

    def promote(
        self,
        *,
        now_utc: datetime,
        telegram_ids: set[int] | None = None,
        outbox_id: int | None = None,
    ) -> DeliveryReport:
        now = _utc(now_utc)
        settings = self.store.load_settings()
        targets = self._targets(settings, telegram_ids)
        report = DeliveryReport()
        gate = self._global_gate(settings)
        if gate:
            report.blocked.append(gate)
            return report
        for row in self.events.outbox_records(status="shadow"):
            if row.telegram_id not in targets:
                continue
            if outbox_id is not None and row.outbox_id != outbox_id:
                continue
            eligibility = self._eligibility(row, settings)
            if eligibility.eligible:
                report.promoted += int(
                    self.events.promote_shadow(row.outbox_id, now_utc=now)
                )
            elif eligibility.permanent:
                report.cancelled += int(
                    self.events.cancel_outbox(
                        row.outbox_id, now_utc=now, reason=eligibility.reason
                    )
                )
            elif eligibility.reason not in report.blocked:
                report.blocked.append(eligibility.reason)
        return report

    def deliver_due(
        self,
        *,
        now_utc: datetime,
        telegram_ids: set[int] | None = None,
        outbox_id: int | None = None,
        max_messages: int = 10,
    ) -> DeliveryReport:
        now = _utc(now_utc)
        settings = self.store.load_settings()
        targets = self._targets(settings, telegram_ids)
        report = DeliveryReport()
        gate = self._global_gate(settings)
        if gate:
            report.blocked.append(gate)
            return report
        report.reconciled_seen = self._reconcile_seen(targets, now)
        report.stale_sending_recovered = self.events.recover_stale_sending(
            now_utc=now,
            stale_before_utc=now - timedelta(minutes=self.sending_stale_minutes),
            telegram_ids=targets,
        )
        report.uncertain += report.stale_sending_recovered
        self.events.release_due_retries(now_utc=now, telegram_ids=targets)

        # Cancel invalid pending work before the transactional claim. A second
        # eligibility read after claim closes the config-change window before transport.
        for row in self.events.outbox_records(status="pending"):
            if row.telegram_id not in targets or row.not_before > now.isoformat():
                continue
            if outbox_id is not None and row.outbox_id != outbox_id:
                continue
            eligibility = self._eligibility(row, settings)
            if eligibility.permanent:
                report.cancelled += int(
                    self.events.cancel_outbox(
                        row.outbox_id, now_utc=now, reason=eligibility.reason
                    )
                )

        for _ in range(max_messages):
            row = self.events.claim_due_outbox(
                now_utc=now, telegram_ids=targets, outbox_id=outbox_id
            )
            if row is None:
                break
            fresh_settings = self.store.load_settings()
            eligibility = self._eligibility(row, fresh_settings)
            if not eligibility.eligible:
                if eligibility.permanent:
                    report.cancelled += int(
                        self.events.cancel_outbox(
                            row.outbox_id, now_utc=now, reason=eligibility.reason
                        )
                    )
                else:
                    self.events.return_outbox_to_pending(
                        row.outbox_id, now_utc=now, reason=eligibility.reason
                    )
                    if eligibility.reason not in report.blocked:
                        report.blocked.append(eligibility.reason)
                continue
            current_person = self.store.load_allowlist().get(row.telegram_id)
            if current_person is None:  # eligibility already guards this
                self.events.cancel_outbox(
                    row.outbox_id,
                    now_utc=now,
                    reason="recipient disappeared before transport",
                )
                report.cancelled += 1
                continue
            current_due = notification_not_before(current_person.profile, now)
            if current_due > now:
                self.events.return_outbox_to_pending(
                    row.outbox_id,
                    now_utc=now,
                    not_before_utc=current_due,
                    reason="recipient is currently in quiet hours",
                )
                if "recipient is currently in quiet hours" not in report.blocked:
                    report.blocked.append("recipient is currently in quiet hours")
                continue
            try:
                receipt = self.sender.send(
                    telegram_id=row.telegram_id, text=row.message_text
                )
            except TelegramSendError as exc:
                if exc.ambiguous:
                    status = self.events.mark_outbox_failure(
                        row.outbox_id,
                        now_utc=now,
                        error_class="telegram_ambiguous",
                        error_message=str(exc),
                        retry_at_utc=None,
                        uncertain=True,
                    )
                elif exc.terminal or row.attempts >= self.max_attempts:
                    status = self.events.mark_outbox_failure(
                        row.outbox_id,
                        now_utc=now,
                        error_class="telegram_terminal",
                        error_message=str(exc),
                        retry_at_utc=None,
                    )
                else:
                    delay = exc.retry_after or min(
                        3600, self.retry_base_seconds * (2 ** (row.attempts - 1))
                    )
                    status = self.events.mark_outbox_failure(
                        row.outbox_id,
                        now_utc=now,
                        error_class="telegram_retryable",
                        error_message=str(exc),
                        retry_at_utc=now + timedelta(seconds=delay),
                    )
                setattr(report, status, getattr(report, status) + 1)
                continue
            except Exception as exc:
                self.events.mark_outbox_failure(
                    row.outbox_id,
                    now_utc=now,
                    error_class="transport_unknown",
                    error_message=f"{type(exc).__name__}: {exc}",
                    retry_at_utc=None,
                    uncertain=True,
                )
                report.uncertain += 1
                continue

            self.events.mark_outbox_sent(
                row.outbox_id,
                now_utc=now,
                telegram_message_id=_receipt_message_id(receipt),
            )
            try:
                listing = Listing.model_validate_json(
                    self.events.outbox_listing_json(row.outbox_id)
                )
                self.store.mark_seen(
                    row.telegram_id,
                    row.identity_key,
                    search_name=row.primary_search_name,
                    url=listing.url,
                    now_utc=now,
                )
            except Exception:
                # The durable receipt is authoritative. Reconciliation on the
                # next worker pass repairs the legacy daily seen ledger.
                pass
            report.sent += 1
            report.sent_outbox_ids.append(row.outbox_id)
        return report

    def run(
        self,
        *,
        now_utc: datetime,
        telegram_ids: set[int] | None = None,
        outbox_id: int | None = None,
        max_messages: int = 10,
    ) -> DeliveryReport:
        promoted = self.promote(
            now_utc=now_utc, telegram_ids=telegram_ids, outbox_id=outbox_id
        )
        delivered = self.deliver_due(
            now_utc=now_utc,
            telegram_ids=telegram_ids,
            outbox_id=outbox_id,
            max_messages=max_messages,
        )
        promoted.sent = delivered.sent
        promoted.retry_wait = delivered.retry_wait
        promoted.failed = delivered.failed
        promoted.uncertain += delivered.uncertain
        promoted.cancelled += delivered.cancelled
        promoted.reconciled_seen = delivered.reconciled_seen
        promoted.stale_sending_recovered = delivered.stale_sending_recovered
        promoted.blocked.extend(
            item for item in delivered.blocked if item not in promoted.blocked
        )
        promoted.sent_outbox_ids = delivered.sent_outbox_ids
        return promoted
