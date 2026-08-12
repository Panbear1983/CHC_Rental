"""Durable, source-only incremental collection cycle.

This module starts or resumes paid Apify work and returns canonical records. It
does not match recipients or send Telegram messages; those remain later phases.
All live collection is gated by config-v2 readiness, alert-ledger readiness,
the global incremental kill switch, the Zillow source flag and existing quotas.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from chc_rental.event_store import QueryScopeRecord, SourceRunRecord
from chc_rental.models import Settings
from chc_rental.sources.apify import ApifyClient, ApifyRunState
from chc_rental.sources.base import SourceError
from chc_rental.sources.planner import PlannedSourceQuery, plan_incremental_queries
from chc_rental.sources.zillow import ZillowRentalAdapter
from chc_rental.store import Store


@dataclass
class IncrementalCollection:
    query_id: str
    run_id: str | None
    status: str
    records: list[dict[str, Any]] = field(default_factory=list)
    raw_result_count: int = 0
    truncated: bool = False
    cost_usd: float | None = None
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "run_id": self.run_id,
            "status": self.status,
            "records": len(self.records),
            "raw_result_count": self.raw_result_count,
            "truncated": self.truncated,
            "cost_usd": self.cost_usd,
            "error": self.error,
        }


@dataclass
class IncrementalCycleReport:
    planned_scopes: int = 0
    started: int = 0
    resumed: int = 0
    warnings: list[str] = field(default_factory=list)
    collections: list[IncrementalCollection] = field(default_factory=list)

    @property
    def records(self) -> list[dict[str, Any]]:
        return [record for collection in self.collections for record in collection.records]

    def summary(self) -> dict[str, Any]:
        return {
            "planned_scopes": self.planned_scopes,
            "started": self.started,
            "resumed": self.resumed,
            "warnings": self.warnings,
            "collections": [item.summary() for item in self.collections],
            "records": len(self.records),
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("incremental cycle time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _inside_active_window(settings: Settings, now_utc: datetime) -> bool:
    local = now_utc.astimezone(ZoneInfo(settings.scrape_timezone))
    current = (local.hour, local.minute)
    start = tuple(map(int, settings.incremental_active_start.split(":")))
    end = tuple(map(int, settings.incremental_active_end.split(":")))
    if start < end:
        return start <= current < end
    return current >= start or current < end


class IncrementalCollector:
    """One scheduler cycle for the initial Zillow incremental source."""

    def __init__(
        self,
        store: Store,
        *,
        settings: Settings,
        adapter: ZillowRentalAdapter,
        client: ApifyClient,
        worker_id: str | None = None,
        allow_disabled: bool = False,
        meter_requests: bool = True,
    ) -> None:
        self.store = store
        self.events = store.event_store()
        self.settings = settings
        self.adapter = adapter
        self.client = client
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.allow_disabled = allow_disabled
        self.meter_requests = meter_requests

    def cycle(self, *, now_utc: datetime) -> IncrementalCycleReport:
        now = _utc(now_utc)
        if not self.store.config_v2_status()["ready"]:
            raise ValueError("incremental config migration is required")
        if not self.store.alert_migration_status().ready:
            raise ValueError("incremental alert ledger migration is required")
        report = IncrementalCycleReport()
        planned, warnings = plan_incremental_queries(
            self.store.load_allowlist(), source="zillow"
        )
        report.planned_scopes = len(planned)
        report.warnings.extend(warnings)
        self.events.sync_query_scopes(
            [
                {
                    "query_id": item.query_id,
                    "source": item.source,
                    "query_json": item.query_json,
                }
                for item in planned
            ],
            now_utc=now,
        )
        planned_by_id = {item.query_id: item for item in planned}

        if not self.settings.incremental_alerts_enabled and not self.allow_disabled:
            report.warnings.append("incremental alerts are disabled by the global kill switch")
            return report
        if not self.settings.zillow_enabled and not self.allow_disabled:
            report.warnings.append("Zillow incremental source is disabled")
            return report

        open_runs = self.events.open_source_runs()
        for run in open_runs:
            report.resumed += 1
            report.collections.append(self._resume(run, planned_by_id, now))

        if not _inside_active_window(self.settings, now):
            report.warnings.append("outside the incremental collection active window")
            return report

        still_open = {run.query_id for run in self.events.open_source_runs()}
        starts_left = self.settings.incremental_max_new_starts_per_cycle
        for scope in self.events.due_query_scopes(now_utc=now):
            if starts_left <= 0:
                break
            if scope.query_id in still_open:
                continue
            planned_scope = planned_by_id.get(scope.query_id)
            if planned_scope is None:
                continue
            if not self.events.claim_query_scope(
                scope.query_id,
                owner=self.worker_id,
                now_utc=now,
                lease_minutes=self.settings.incremental_query_lease_minutes,
            ):
                continue
            collection = self._start(scope, planned_scope, now)
            report.collections.append(collection)
            if collection.status != "budget_deferred":
                report.started += 1
                starts_left -= 1
        return report

    def _start(
        self,
        scope: QueryScopeRecord,
        planned: PlannedSourceQuery,
        now: datetime,
    ) -> IncrementalCollection:
        interval = timedelta(minutes=self.settings.zillow_incremental_interval_minutes)
        try:
            payload = self.adapter.actor_input(planned.query)
        except SourceError as exc:
            self.events.release_query_scope(
                scope.query_id,
                now_utc=now,
                next_due_at=now + min(interval, timedelta(minutes=60)),
                succeeded=False,
            )
            return IncrementalCollection(
                query_id=scope.query_id,
                run_id=None,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

        if self.meter_requests and not self.store.reserve_request(
            now.date(),
            "zillow",
            per_source_limit=self.settings.source_request_budget("zillow"),
            global_limit=self.settings.global_daily_request_budget,
        ):
            self.events.defer_query_scope(
                scope.query_id,
                now_utc=now,
                next_due_at=now + timedelta(minutes=self.settings.incremental_scheduler_tick_minutes),
            )
            return IncrementalCollection(
                query_id=scope.query_id,
                run_id=None,
                status="budget_deferred",
                error="Zillow or global daily request budget exhausted",
            )

        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        run_id = str(uuid4())
        self.events.create_source_run(
            run_id=run_id,
            query_id=scope.query_id,
            source="zillow",
            input_fingerprint=fingerprint,
            now_utc=now,
        )
        try:
            remote = self.client.start_actor(
                self.settings.zillow_actor,
                payload,
                max_total_charge_usd=self.settings.zillow_max_charge_usd,
            )
            self.events.attach_apify_run(
                run_id,
                apify_run_id=remote.run_id,
                default_dataset_id=remote.default_dataset_id,
                now_utc=now,
            )
        except SourceError as exc:
            # A network error can occur after Apify accepted the run but before
            # the response reached us. Do not immediately retry this uncertain
            # start; wait a full interval and surface it to the operator.
            self.events.finish_source_run(
                run_id,
                status="failed",
                now_utc=now,
                error_class="start_uncertain",
                error_message=f"{type(exc).__name__}: {str(exc)[:500]}",
            )
            self.events.release_query_scope(
                scope.query_id,
                now_utc=now,
                next_due_at=now + interval,
                succeeded=False,
            )
            return IncrementalCollection(
                query_id=scope.query_id,
                run_id=run_id,
                status="start_uncertain",
                error=f"{type(exc).__name__}: {exc}",
            )
        return self._process_remote(
            self.events.source_run(run_id), remote, now
        )

    def _resume(
        self,
        run: SourceRunRecord,
        planned_by_id: dict[str, PlannedSourceQuery],
        now: datetime,
    ) -> IncrementalCollection:
        interval = timedelta(minutes=self.settings.zillow_incremental_interval_minutes)
        if run.status == "reserved" or not run.apify_run_id:
            self.events.finish_source_run(
                run.run_id,
                status="failed",
                now_utc=now,
                error_class="start_uncertain",
                error_message="local reservation survived without a persisted Apify run id",
            )
            self.events.release_query_scope(
                run.query_id,
                now_utc=now,
                next_due_at=now + interval,
                succeeded=False,
            )
            return IncrementalCollection(
                query_id=run.query_id,
                run_id=run.run_id,
                status="start_uncertain",
                error="reserved run had no persisted Apify run id",
            )
        try:
            remote = self.client.get_run(run.apify_run_id)
        except SourceError as exc:
            # Leave the run open: a read failure says nothing about remote state.
            return IncrementalCollection(
                query_id=run.query_id,
                run_id=run.run_id,
                status="poll_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return self._process_remote(run, remote, now)

    def _process_remote(
        self,
        run: SourceRunRecord | None,
        remote: ApifyRunState,
        now: datetime,
    ) -> IncrementalCollection:
        if run is None:  # pragma: no cover - defensive local corruption guard
            raise ValueError("local source run disappeared")
        interval = timedelta(minutes=self.settings.zillow_incremental_interval_minutes)
        if not remote.terminal:
            return IncrementalCollection(
                query_id=run.query_id,
                run_id=run.run_id,
                status="running",
                cost_usd=remote.usage_total_usd,
            )
        if not remote.succeeded:
            terminal = "timed_out" if remote.status == "TIMED-OUT" else (
                "cancelled" if remote.status == "ABORTED" else "failed"
            )
            self.events.finish_source_run(
                run.run_id,
                status=terminal,
                now_utc=now,
                error_class=f"apify_{remote.status.lower()}",
                error_message=remote.status_message,
                charge_usd=remote.usage_total_usd,
                charge_known=remote.usage_total_usd is not None,
            )
            self.events.release_query_scope(
                run.query_id,
                now_utc=now,
                next_due_at=now + min(interval, timedelta(minutes=60)),
                succeeded=False,
            )
            return IncrementalCollection(
                query_id=run.query_id,
                run_id=run.run_id,
                status=terminal,
                cost_usd=remote.usage_total_usd,
                error=remote.status_message or remote.status,
            )
        dataset_id = remote.default_dataset_id or run.default_dataset_id
        if not dataset_id:
            error = "successful Apify run had no default dataset id"
            self.events.finish_source_run(
                run.run_id,
                status="failed",
                now_utc=now,
                error_class="missing_dataset",
                error_message=error,
                charge_usd=remote.usage_total_usd,
                charge_known=remote.usage_total_usd is not None,
            )
            self.events.release_query_scope(
                run.query_id,
                now_utc=now,
                next_due_at=now + min(interval, timedelta(minutes=60)),
                succeeded=False,
            )
            return IncrementalCollection(
                query_id=run.query_id,
                run_id=run.run_id,
                status="failed",
                cost_usd=remote.usage_total_usd,
                error=error,
            )
        try:
            raw = self.client.get_dataset(dataset_id)
            records = self.adapter.normalize_dataset(raw)
        except SourceError as exc:
            # Dataset reads are safe to retry; leave the paid run open.
            return IncrementalCollection(
                query_id=run.query_id,
                run_id=run.run_id,
                status="dataset_failed",
                cost_usd=remote.usage_total_usd,
                error=f"{type(exc).__name__}: {exc}",
            )
        truncated = len(raw) >= self.adapter.results_limit
        self.events.finish_source_run(
            run.run_id,
            status="succeeded",
            now_utc=now,
            result_count=len(raw),
            truncated=truncated,
            charge_usd=remote.usage_total_usd,
            charge_known=remote.usage_total_usd is not None,
        )
        self.events.release_query_scope(
            run.query_id,
            now_utc=now,
            next_due_at=now + interval,
            succeeded=True,
        )
        return IncrementalCollection(
            query_id=run.query_id,
            run_id=run.run_id,
            status="succeeded",
            records=records,
            raw_result_count=len(raw),
            truncated=truncated,
            cost_usd=remote.usage_total_usd,
        )
