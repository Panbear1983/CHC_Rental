"""Daily fetch orchestration: scrape-time gate, day-cache, quota, retries.

The launchd job checks frequently while the product is a DAILY digest, and
every source request costs money. This module is what reconciles the two:

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
a dishonest no-results notice on later delivery checks.

``usable`` distinguishes "the market really had nothing" from "the source was
down": a sweep where every query failed must NOT flow onward, or people with
``notify_on_no_results`` would be told there are no rentals when in truth we
simply couldn't look.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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

MAX_RATE_LIMIT_WAIT = 30.0  # seconds; an unattended source run must not stall long

# How many pages (each one request) to pull per city before stopping. A dense
# market like Brooklyn has thousands of active rentals; fetching all of them
# would burn the daily request budget on one city and, on the free RentCast
# tier (50 requests/MONTH), the whole month in a couple of runs. We take the
# first few pages and stop — reaching this cap is a normal "there is more we
# chose not to fetch", NOT a failure. Raise it (or move to server-side
# bed/bath filtering) if a city's newest listings don't fit in this slice.
MAX_PAGES_PER_QUERY = 5

# Share of IN-CITY priced results allowed outside their rent/bed envelope before
# the run warns. A correctly applied source filter leaves only Zillow's own
# rounding and range cards outside, well under this; anything above it means the
# filter is not reaching the search and paid slots are being spent on listings
# no watcher asked for. Out-of-city results are deliberately NOT counted here —
# see _envelope_audit.
ENVELOPE_MISS_ALERT_SHARE = 0.20


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


def scrape_day(settings: Settings, now_utc: datetime) -> date:
    """Calendar day used by the daily cache and request quota.

    The scrape clock is configured in ``settings.scrape_timezone``. Keying the
    same cache by UTC used to roll the day over at 20:00 New York in summer,
    which caused an evening scrape and made the intended 06:00 scrape a cache
    hit. The cache, quota, and delivery-only reader must share this one day.
    """
    return now_utc.astimezone(ZoneInfo(settings.scrape_timezone)).date()


def _query_scope(queries: Iterable[SourceQuery]) -> list[dict[str, str]]:
    """Stable cache identity for the cities represented by one source pool."""
    return sorted(
        (
            {"city": query.city.strip().casefold(), "state": query.state.strip().upper()}
            for query in queries
        ),
        key=lambda item: (item["city"], item["state"]),
    )


def _envelope_audit(
    queries: Iterable[SourceQuery], records: Iterable[Any]
) -> dict[str, int]:
    """Split paid results into the two DIFFERENT ways a slot gets wasted.

    ``outside_filters`` — the record's city was asked for, but its rent or bed
    count was not. This is the regression signal: a source-side filter that
    stops applying produces no error anywhere, local matching just rejects more
    quietly, and the only symptom is fewer pushes. That is how the rent
    envelope went unapplied from the first Zillow run through 2026-08-28,
    spending 64% of every paid result on listings outside the band.

    ``outside_city`` — the record is in a city nobody watches. This is
    STRUCTURAL, not a regression: the actor needs a rectangular map bound, and
    Nominatim's rectangle for Brooklyn also covers Lower Manhattan, part of
    Jersey City and western Queens. Measured 2026-08-29, it is ~27% of a run.

    They are counted apart on purpose. Folded together, the known structural
    27% sits permanently above any sane threshold and drowns the regression
    signal the warning exists to carry.
    """
    planned = list(queries)
    audit = {"comparable": 0, "outside_city": 0, "outside_filters": 0}
    for record in records:
        if not isinstance(record, dict):
            continue
        price = record.get("price")
        if not isinstance(price, int) or isinstance(price, bool):
            continue
        audit["comparable"] += 1
        in_scope = [query for query in planned if _matches_place(record, query)]
        if not in_scope:
            audit["outside_city"] += 1
        elif not any(_within_filters(record, query) for query in in_scope):
            audit["outside_filters"] += 1
    return audit


def _matches_place(record: dict[str, Any], query: SourceQuery) -> bool:
    """City and state agreement, the same way ``matching.py`` decides it."""
    return (
        str(record.get("city") or "").strip().casefold()
        == query.city.strip().casefold()
        and str(record.get("state") or "").strip().upper() == query.state.strip().upper()
    )


def _within_filters(record: dict[str, Any], query: SourceQuery) -> bool:
    """Rent and bed agreement for a record already known to be in-city."""
    price = record.get("price")
    if query.price_min is not None and price < query.price_min:
        return False
    if query.price_max is not None and price > query.price_max:
        return False
    beds = record.get("beds")
    if isinstance(beds, int) and not isinstance(beds, bool):
        if query.beds_min is not None and beds < query.beds_min:
            return False
        if query.beds_max is not None and beds > query.beds_max:
            return False
    return True


def _cache_matches_scrape_day(
    store: Store,
    day: date,
    source: str,
    settings: Settings,
    metadata: dict[str, Any],
) -> bool:
    """Reject snapshots filed under a UTC date by the pre-migration runner.

    New snapshots carry an explicit day and timezone. For legacy snapshots we
    use the cache file's mtime as a conservative migration check: it must have
    been written on ``day`` in the configured scrape zone and at/after the
    scrape clock. This preserves correctly filed historical caches while
    rejecting, for example, a 20:01 New York scrape stored as tomorrow's UTC
    date.
    """
    marked_day = metadata.get("scrape_day")
    marked_zone = metadata.get("scrape_timezone")
    if marked_day is not None or marked_zone is not None:
        return marked_day == day.isoformat() and marked_zone == settings.scrape_timezone

    try:
        stamp = datetime.fromtimestamp(
            store.cache_path(day, source).stat().st_mtime,
            tz=timezone.utc,
        ).astimezone(ZoneInfo(settings.scrape_timezone))
    except (OSError, OverflowError, ValueError):
        return False
    hour, minute = map(int, settings.scrape_time.split(":"))
    return stamp.date() == day and (stamp.hour, stamp.minute) >= (hour, minute)


def _cached_report(
    store: Store,
    source: str,
    *,
    settings: Settings,
    day: date,
    queries: list[SourceQuery],
    warnings: Iterable[str],
) -> FetchReport:
    """Load one source snapshot without constructing or calling an adapter."""
    report = FetchReport(source=source)
    report.warnings.extend(warnings)
    report.queries_planned = len(queries)
    if not queries:
        report.warnings.append(
            "no fetchable searches (each needs city + state); nothing to load"
        )
        return report

    query_scope = _query_scope(queries)
    cached = store.load_cached(day, source, expected_query_scope=query_scope)
    metadata = store.load_cache_metadata(day, source) or {}
    if cached is None or not _cache_matches_scrape_day(
        store, day, source, settings, metadata
    ):
        report.warnings.append(
            f"no compatible {source} cache for scrape day {day}; "
            "delivery-only check will not fetch"
        )
        return report

    report.records = cached
    report.from_cache = True
    report.queries_completed = int(metadata.get("queries_completed", len(queries)))
    report.truncated = bool(metadata.get("truncated", False))
    report.errors.extend(str(item) for item in metadata.get("errors", []))
    report.warnings.extend(str(item) for item in metadata.get("warnings", []))
    return report


def load_daily_cached(
    store: Store,
    source: str,
    *,
    now_utc: datetime,
) -> FetchReport:
    """Read today's compatible pool for ``source`` with zero network access."""
    settings = store.load_settings()
    queries, warnings = plan_queries(store.load_allowlist())
    return _cached_report(
        store,
        source,
        settings=settings,
        day=scrape_day(settings, now_utc),
        queries=queries,
        warnings=warnings,
    )


def load_many_daily_cached(
    store: Store,
    sources: Iterable[str],
    *,
    now_utc: datetime,
    configuration_warnings: Iterable[str] = (),
) -> PoolFetchReport:
    """Combine saved source pools without loading credentials or adapters."""
    pool = PoolFetchReport(configuration_warnings=list(configuration_warnings))
    seen_names: set[str] = set()
    for source in sources:
        if source in seen_names:
            pool.configuration_warnings.append(
                f"duplicate cached source {source!r} was ignored"
            )
            continue
        seen_names.add(source)
        report = load_daily_cached(store, source, now_utc=now_utc)
        pool.sources.append(report)
        if report.usable:
            pool.records.extend(report.records)
    return pool


def fetch_daily(
    store: Store,
    adapter: SourceAdapter,
    *,
    now_utc: datetime,
    sleeper=time.sleep,
) -> FetchReport:
    """Return today's listing pool, fetching from the network at most once per day."""
    settings = store.load_settings()
    day = scrape_day(settings, now_utc)
    queries, warnings = plan_queries(store.load_allowlist())
    report = FetchReport(source=adapter.name)
    report.warnings.extend(warnings)
    report.queries_planned = len(queries)
    if not queries:
        # Deliberately NOT cached: the moment a fetchable search is saved, the
        # next scheduler check should fetch instead of replaying an empty day.
        report.warnings.append("no fetchable searches (each needs city + state); nothing to fetch")
        return report

    query_scope = _query_scope(queries)
    cached_report = _cached_report(
        store,
        adapter.name,
        settings=settings,
        day=day,
        queries=queries,
        warnings=warnings,
    )
    if cached_report.usable:
        return cached_report

    if not is_scrape_time(settings, now_utc):
        report.warnings.append(
            f"before scrape time {settings.scrape_time} {settings.scrape_timezone}; not fetching"
        )
        return report

    blocked = store.source_blocked_reason(day, adapter.name)
    if blocked is not None:
        # A credential the source already rejected today cannot start working
        # before the day rolls over, and every retry costs a request slot.
        # The ERROR stays byte-identical to the failure that tripped the breaker
        # so the run summary keeps one stable signature and the operator alert
        # pages once per streak rather than once per tick; the note below is
        # what tells the log this tick was suppressed rather than re-attempted.
        report.errors.append(blocked)
        report.warnings.append(
            f"{adapter.name} already failed authentication today; not retried until {day} rolls over"
        )
        return report

    records: list[Any] = []
    try:
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
    except SourceAuthError as exc:
        # Rejected credentials end the sweep, but the report must still carry
        # what was already fetched and the requests it really cost. Rebuilding a
        # blank report here used to claim requests_used=0 for reservations that
        # had already been spent, so the run log understated real spend.
        report.truncated = True
        report.errors.append(str(exc))
        store.block_source_for_day(day, adapter.name, reason=str(exc))

    report.fetched = True
    report.records = records
    audit = _envelope_audit(queries, records)
    comparable = audit["comparable"]
    miss_share = (audit["outside_filters"] / comparable) if comparable else None
    if miss_share is not None and miss_share > ENVELOPE_MISS_ALERT_SHARE:
        report.warnings.append(
            f"{audit['outside_filters']}/{comparable} priced results ({miss_share:.0%}) "
            "were in a watched city but outside its rent/bed envelope; the "
            "source-side filter may not be applying"
        )
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
                "scrape_day": day.isoformat(),
                "scrape_timezone": settings.scrape_timezone,
                "scraped_at_utc": now_utc.astimezone(timezone.utc).isoformat(),
                "queries_planned": report.queries_planned,
                "queries_completed": report.queries_completed,
                "envelope_miss_share": miss_share,
                "envelope_audit": audit,
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
            # Rejected credentials cost no money and no provider quota. Give the
            # reservation back before re-raising, then let fetch_daily trip the
            # day breaker so this is attempted once, not every ten minutes.
            store.refund_request(day, adapter.name)
            report.requests_used -= 1
            raise
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
