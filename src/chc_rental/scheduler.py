"""One restart-safe unattended incremental tick; LaunchAgent policy lives outside."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from chc_rental.delivery_worker import DeliveryWorker, IncrementalSender
from chc_rental.incremental import IncrementalCollector
from chc_rental.outbox import process_incremental_report
from chc_rental.store import Store


@dataclass
class SchedulerReport:
    skipped_locked: bool = False
    source: dict[str, Any] | None = None
    processing: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
    backup: str | None = None
    pruned: dict[str, int] | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "skipped_locked": self.skipped_locked,
            "source": self.source,
            "processing": self.processing,
            "delivery": self.delivery,
            "backup": self.backup,
            "pruned": self.pruned,
            "warnings": self.warnings,
        }


class IncrementalScheduler:
    def __init__(
        self,
        store: Store,
        *,
        collector: IncrementalCollector | None,
        sender: IncrementalSender | None,
        owner_alert: Callable[[str], bool] | None = None,
        source_unavailable_reason: str | None = None,
    ) -> None:
        self.store = store
        self.collector = collector
        self.sender = sender
        self.owner_alert = owner_alert
        self.source_unavailable_reason = source_unavailable_reason

    def _alert_operational_changes(self, now: datetime, report: SchedulerReport) -> None:
        events = self.store.event_store()
        for breaker in events.pending_source_alerts():
            if breaker.pending_alert == "recovered":
                text = f"{breaker.source} incremental source recovered"
            elif breaker.pending_alert == "failure_started":
                text = (
                    f"{breaker.source} incremental source failure started: "
                    f"{breaker.last_error_class or 'unknown'}"
                )
            else:
                text = (
                    f"{breaker.source} source breaker {breaker.pending_alert}: "
                    f"{breaker.last_error_class or 'unknown'}"
                )
            if self.owner_alert is not None and self.owner_alert(text):
                events.acknowledge_source_alert(breaker.source, now_utc=now)

        terminal = events.terminal_failures_needing_alert()
        owner_id = self.store.load_settings().owner_telegram_id
        alertable = [row for row in terminal if row.telegram_id != owner_id]
        if alertable:
            text = "incremental delivery needs review: " + ", ".join(
                f"outbox {row.outbox_id}={row.status}" for row in alertable[:10]
            )
            if self.owner_alert is not None and self.owner_alert(text):
                events.mark_terminal_failure_alerted(
                    [row.outbox_id for row in alertable], now_utc=now
                )
        if terminal and not alertable:
            report.warnings.append(
                "owner-recipient delivery failures require dashboard review"
            )

    def tick(self, *, now_utc: datetime, max_delivery_messages: int = 10) -> SchedulerReport:
        if now_utc.tzinfo is None or now_utc.utcoffset() is None:
            raise ValueError("scheduler time must be timezone-aware")
        now = now_utc.astimezone(timezone.utc)
        report = SchedulerReport()
        with self.store.try_run_lock() as acquired:
            if not acquired:
                report.skipped_locked = True
                report.warnings.append("another daily or incremental workflow owns the run lock")
                return report
            settings = self.store.load_settings()
            if self.collector is not None:
                try:
                    source = self.collector.cycle(now_utc=now)
                    report.source = source.summary()
                    processing = process_incremental_report(
                        self.store, source, now_utc=now
                    )
                    report.processing = processing.summary()
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    report.warnings.append(f"incremental source cycle failed: {reason}")
                    self.store.event_store().record_source_failure(
                        "zillow",
                        now_utc=now,
                        error_class="scheduler_source_error",
                        error_message=reason,
                        threshold=settings.incremental_source_breaker_failures,
                        cooldown_minutes=(
                            settings.incremental_source_breaker_cooldown_minutes
                        ),
                    )
            elif (
                settings.incremental_alerts_enabled
                and settings.zillow_enabled
                and self.source_unavailable_reason
            ):
                report.warnings.append(self.source_unavailable_reason)
                self.store.event_store().record_source_failure(
                    "zillow",
                    now_utc=now,
                    error_class="missing_credential",
                    error_message=self.source_unavailable_reason,
                    threshold=settings.incremental_source_breaker_failures,
                    cooldown_minutes=settings.incremental_source_breaker_cooldown_minutes,
                    immediate_open=True,
                )

            if self.sender is not None:
                report.delivery = DeliveryWorker(
                    self.store, sender=self.sender
                ).run(
                    now_utc=now,
                    max_messages=max_delivery_messages,
                ).summary()
            elif settings.live_push_enabled and settings.incremental_canary_telegram_ids:
                report.warnings.append(
                    "Telegram sender is unavailable; queued delivery was not attempted"
                )

            self._alert_operational_changes(now, report)
            try:
                report.backup = str(
                    self.store.event_store().create_daily_backup(now_utc=now)
                )
            except Exception as exc:
                report.warnings.append(
                    f"alert-ledger backup failed: {type(exc).__name__}: {exc}"
                )
            report.pruned = self.store.prune(today=now.date(), settings=settings)
            incremental_removed = self.store.event_store().prune_history(
                before_utc=now
                - timedelta(days=settings.incremental_event_retention_days)
            )
            report.pruned.update(
                {f"incremental_{key}": value for key, value in incremental_removed.items()}
            )
        return report
