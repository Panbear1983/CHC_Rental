"""Daily fetch orchestration: scrape-time gate, day-cache, quota, retries.

The launchd job runs HOURLY while the product is a DAILY digest, and every
source request costs money. This module is what reconciles the two:

* **Query-aware day cache.** The first sweep of a day that actually spends
  requests is written to ``state/cache/<day>/<source>.json`` with a sidecar
  describing its city/state scope and coverage. Later runs reuse it for free
  only while that scope still matches the active allowlist. Adding/removing a
  city invalidates the pool; editing local price/bed filters does not.
* **Scrape-time gate.** Before ``settings.scrape_time`` (in
  ``settings.scrape_timezone``) no network fetch happens at all, so overnight
  runs cost nothing and the day's data is fetched once, in the morning,
  before the earliest delivery.
* **Budget.** Every page request — retries included — reserves one unit via
  ``store.reserve_request``, which enforces the per-source and global daily
  ceilings atomically. A refused reservation ends the sweep
  (``truncated=True``): a hard stop, exactly as Decision #8 requires.
* **Retry.** One retry per page on 429 (honoring Retry-After, capped) or a
  transient failure. Auth failures abort the whole sweep, because retrying a
  bad key can never help.

Truncation/errors are preserved in cache metadata. A partial pool remains
usable for positive matches but is never called complete, so it cannot produce
a dishonest no-results notice on later hourly runs.

``usable`` distinguishes "the market really had nothing" from "the source was
down": a sweep where every query failed must NOT flow onward, or people with
``notify_on_no_results`` would be told there are no rentals when in truth we
simply couldn't look.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from chc_rental.models import Settings
from chc_rental.sources.base import (
    SourceAdapter,
    SourceAuthError,
    SourceQuery,
    SourceRateLimitError,
    SourceUnavailableError,
)
from chc_rental.sources.planner import plan_queries
from chc_rental.store import Store

MAX_RATE_LIMIT_WAIT = 30.0  # seconds; an unattended hourly run must not stall long

# How many pages (each one request) to pull per city before stopping. A dense
# market like Brooklyn has thousands of active rentals; fetching all of them
# would burn the daily request budget on one city and, on the free RentCast
# tier (50 requests/MONTH), the whole month in a couple of runs. We take the
# first few pages and stop — reaching this cap is a normal "there is more we
# chose not to fetch", NOT a failure. Raise it (or move to server-side
# bed/bath filtering) if a city's newest listings don't fit in this slice.
MAX_PAGES_PER_QUERY = 5


@dataclass
class FetchReport:
    source: str
    records: list[Any] = field(default_factory=list)
    from_cache: bool = False
    fetched: bool = False
    requests_used: int = 0
    queries_planned: int = 0
    queries_completed: int = 0
    truncated: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when this run holds a listing pool it is honest to act on."""
        if self.from_cache:
            return True
        return self.fetched and self.queries_completed > 0

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "from_cache": self.from_cache,
            "fetched": self.fetched,
            "requests_used": self.requests_used,
            "queries_planned": self.queries_planned,
            "queries_completed": self.queries_completed,
            "records": len(self.records),
            "truncated": self.truncated,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class PoolFetchReport:
    """All source outcomes and the combined canonical-record pool for one run."""

    sources: list[FetchReport] = field(default_factory=list)
    records: list[Any] = field(default_factory=list)
    configuration_warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return any(report.usable for report in self.sources)

    @property
    def complete(self) -> bool:
        """True only when every source provided its whole intended coverage."""
        return bool(self.sources) and not self.configuration_warnings and all(
            report.usable and not report.truncated and not report.errors
            for report in self.sources
        )

    def summary(self) -> dict[str, Any]:
        return {
            "sources": [report.summary() for report in self.sources],
            "records": len(self.records),
            "usable": self.usable,
            "complete": self.complete,
            "configuration_warnings": self.configuration_warnings,
        }


def is_scrape_time(settings: Settings, now_utc: datetime) -> bool:
    local = now_utc.astimezone(ZoneInfo(settings.scrape_timezone))
    hour, minute = map(int, settings.scrape_time.split(":"))
    return (local.hour, local.minute) >= (hour, minute)


def _query_scope(queries: Iterable[SourceQuery]) -> list[dict[str, str]]:
    """Stable cache identity for the cities represented by one source pool."""
    return sorted(
        (
            {"city": query.city.strip().casefold(), "state": query.state.strip().upper()}
            for query in queries
        ),
        key=lambda item: (item["city"], item["state"]),
    )


def fetch_daily(
    store: Store,
    adapter: SourceAdapter,
    *,
    now_utc: datetime,
    sleeper=time.sleep,
) -> FetchReport:
    """Return today's listing pool, fetching from the network at most once per day."""
    day = now_utc.date()
    report = FetchReport(source=adapter.name)
    settings = store.load_settings()
    queries, warnings = plan_queries(store.load_allowlist())
    report.warnings.extend(warnings)
    report.queries_planned = len(queries)
    if not queries:
        # Deliberately NOT cached: the moment a fetchable search is saved, the
        # next hourly run should fetch instead of replaying an empty day.
        report.warnings.append("no fetchable searches (each needs city + state); nothing to fetch")
        return report

    query_scope = _query_scope(queries)
    cached = store.load_cached(
        day, adapter.name, expected_query_scope=query_scope
    )
    if cached is not None:
        report.records = cached
        report.from_cache = True
        metadata = store.load_cache_metadata(day, adapter.name) or {}
        report.queries_completed = int(metadata.get("queries_completed", len(queries)))
        report.truncated = bool(metadata.get("truncated", False))
        report.errors.extend(str(item) for item in metadata.get("errors", []))
        report.warnings.extend(str(item) for item in metadata.get("warnings", []))
        return report

    if not is_scrape_time(settings, now_utc):
        report.warnings.append(
            f"before scrape time {settings.scrape_time} {settings.scrape_timezone}; not fetching"
        )
        return report

    records: list[Any] = []
    for query in queries:
        usable, stop_sweep = _sweep_query(
            store, adapter, settings, day, query, records, report, sleeper
        )
        if usable:
            report.queries_completed += 1
        # Only a spent request budget stops the whole sweep. One city hitting
        # its page cap, or one query failing, must not skip the other cities.
        if stop_sweep:
            break

    report.fetched = True
    report.records = records
    if report.requests_used > 0:
        # Cache whatever the sweep produced — even partial. A partial sweep
        # means budget or source trouble, and re-fetching the same pages every
        # hour would repeat the spend for the same answer. Tomorrow is fresh.
        store.cache_raw(
            day,
            adapter.name,
            records,
            metadata={
                "query_scope": query_scope,
                "queries_planned": report.queries_planned,
                "queries_completed": report.queries_completed,
                "truncated": report.truncated,
                "errors": report.errors,
                "warnings": report.warnings,
            },
        )
    return report


def fetch_many_daily(
    store: Store,
    adapters: Iterable[SourceAdapter],
    *,
    now_utc: datetime,
    configuration_warnings: Iterable[str] = (),
    sleeper=time.sleep,
) -> PoolFetchReport:
    """Fetch every source independently and combine every usable pool.

    Authentication or availability failure in one optional source must not
    erase listings already obtained from another source. The aggregate remains
    incomplete, however, so callers can suppress dishonest no-results notices.
    """
    pool = PoolFetchReport(configuration_warnings=list(configuration_warnings))
    seen_names: set[str] = set()
    for adapter in adapters:
        if adapter.name in seen_names:
            pool.configuration_warnings.append(
                f"duplicate source adapter {adapter.name!r} was ignored"
            )
            continue
        seen_names.add(adapter.name)
        try:
            report = fetch_daily(store, adapter, now_utc=now_utc, sleeper=sleeper)
        except SourceAuthError as exc:
            report = FetchReport(
                source=adapter.name,
                fetched=True,
                errors=[str(exc)],
            )
        pool.sources.append(report)
        if report.usable:
            pool.records.extend(report.records)
    return pool


def _reserve(store: Store, adapter: SourceAdapter, settings: Settings, day) -> bool:
    return store.reserve_request(
        day,
        adapter.name,
        per_source_limit=settings.source_request_budget(adapter.name),
        global_limit=settings.global_daily_request_budget,
    )


def _sweep_query(
    store: Store,
    adapter: SourceAdapter,
    settings: Settings,
    day,
    query: SourceQuery,
    records: list[Any],
    report: FetchReport,
    sleeper,
) -> tuple[bool, bool]:
    """Paginate one query into ``records``.

    Returns ``(usable, stop_sweep)``:

    * ``usable`` — this query contributed at least one page, so its records may
      be acted on. A query that hit the page cap with a full slice is usable;
      only a query whose very first page failed is not.
    * ``stop_sweep`` — the request budget is spent; the caller must not start
      any further queries. Reaching the per-city page cap or a single query
      failing does NOT set this — the other cities still get their turn.
    """
    offset = 0
    collected = 0
    for _ in range(MAX_PAGES_PER_QUERY):
        if not _reserve(store, adapter, settings, day):
            report.truncated = True
            report.errors.append(
                f"request budget exhausted before {query.city}, {query.state} finished"
            )
            return collected > 0, True
        report.requests_used += 1
        try:
            page, has_more = _fetch_page_with_retry(
                adapter, query, offset, report, store, settings, day, sleeper
            )
        except SourceAuthError:
            raise  # the caller alerts the operator and aborts the run
        except SourceUnavailableError as exc:
            report.errors.append(f"{query.city}, {query.state}: {exc}")
            return collected > 0, False
        records.extend(page)
        collected += len(page)
        if not has_more or not page:
            return True, False
        offset += len(page)
    # Page cap reached: the market has more than we budgeted to fetch. What we
    # gathered is a valid slice — keep it and move on, don't discard it.
    report.truncated = True
    report.warnings.append(
        f"{query.city}, {query.state}: stopped at the {MAX_PAGES_PER_QUERY}-page cap "
        f"({collected} listings fetched); more exist but were not fetched"
    )
    return True, False


def _fetch_page_with_retry(
    adapter: SourceAdapter,
    query: SourceQuery,
    offset: int,
    report: FetchReport,
    store: Store,
    settings: Settings,
    day,
    sleeper,
):
    """One page, with a single bounded retry for 429/transient failures.

    The caller has already reserved (and counted) the first attempt; the
    retry reserves and counts its own unit, because it IS a second request
    against the source.
    """
    try:
        return adapter.fetch_page(query, offset=offset)
    except SourceRateLimitError as exc:
        wait = min(exc.retry_after or 5.0, MAX_RATE_LIMIT_WAIT)
        report.warnings.append(f"rate limited; waiting {wait:g}s and retrying once")
        sleeper(wait)
    except SourceUnavailableError as exc:
        report.warnings.append(f"transient failure ({exc}); retrying once")
    if not _reserve(store, adapter, settings, day):
        raise SourceUnavailableError("budget exhausted during retry")
    report.requests_used += 1
    try:
        return adapter.fetch_page(query, offset=offset)
    except SourceRateLimitError as exc:
        raise SourceUnavailableError(f"still rate limited after retry: {exc}") from None
