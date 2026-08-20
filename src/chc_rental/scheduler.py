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
    tick_id: int | None = None
    skipped_locked: bool = False
    source: dict[str, Any] | None = None
    processing: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
    backup: str | None = None
    pruned: dict[str, int] | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "tick_id": self.tick_id,
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
        operator_alert: Callable[[str], bool] | None = None,
        source_unavailable_reason: str | None = None,
        runtime_mode: str = "test",
    ) -> None:
        self.store = store
        self.collector = collector
        self.sender = sender
        self.operator_alert = operator_alert
        self.source_unavailable_reason = source_unavailable_reason
        if runtime_mode not in {"live", "fixture", "test"}:
            raise ValueError("scheduler runtime mode must be live, fixture, or test")
        self.runtime_mode = runtime_mode

    @staticmethod
    def _expected_source_warning(message: str) -> bool:
        return message == "outside the incremental collection active window"

    def _record_tick(self, now: datetime, report: SchedulerReport) -> None:
        source = report.source or {}
        collections = source.get("collections") or []
        source_failed = sum(
            item.get("status")
            not in {"succeeded", "running", "budget_deferred"}
            for item in collections
        )
        delivery = report.delivery or {}
        delivery_failed = int(delivery.get("failed", 0)) + int(
            delivery.get("retry_wait", 0)
        )
        delivery_uncertain = int(delivery.get("uncertain", 0))
        source_warnings = [
            str(item)
            for item in source.get("warnings", [])
            if not self._expected_source_warning(str(item))
        ]
        warning_count = len(report.warnings) + len(source_warnings)
        degraded = bool(
            warning_count or source_failed or delivery_failed or delivery_uncertain
        )
        report.tick_id = self.store.event_store().record_scheduler_tick(
            mode=self.runtime_mode,
            status="degraded" if degraded else "ok",
            source_started=int(source.get("started", 0)),
            source_succeeded=sum(
                item.get("status") == "succeeded" for item in collections
            ),
            source_failed=source_failed,
            delivery_sent=int(delivery.get("sent", 0)),
            delivery_failed=delivery_failed,
            delivery_uncertain=delivery_uncertain,
            warning_count=warning_count,
            details={
                "source_warnings": source_warnings,
                "scheduler_warnings": report.warnings,
                "source_statuses": [item.get("status") for item in collections],
            },
            now_utc=now,
        )

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
            if self.operator_alert is not None and self.operator_alert(text):
                events.acknowledge_source_alert(breaker.source, now_utc=now)

        terminal = events.terminal_failures_needing_alert()
        operator_id = self.store.load_settings().operator_alert_telegram_id
        alertable = [row for row in terminal if row.telegram_id != operator_id]
        if alertable:
            text = "incremental delivery needs review: " + ", ".join(
                f"outbox {row.outbox_id}={row.status}" for row in alertable[:10]
            )
            if self.operator_alert is not None and self.operator_alert(text):
                events.mark_terminal_failure_alerted(
                    [row.outbox_id for row in alertable], now_utc=now
                )
        if terminal and not alertable:
            report.warnings.append(
                "operator-recipient delivery failures require dashboard review"
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
            try:
                self._record_tick(now, report)
            except Exception as exc:
                report.warnings.append(
                    f"scheduler evidence write failed: {type(exc).__name__}: {exc}"
                )
        return report
