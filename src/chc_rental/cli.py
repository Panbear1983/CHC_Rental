"""Command-line entry point for the daily run.

Listings come from every enabled source adapter (RentCast plus optional Zillow)
or from a local JSON fixture for offline runs and tests. Fetching is separately
day-cached and budget-capped per source in `chc_rental.fetch`; everything
downstream (validate, match, dedupe, cap, deliver) consumes the combined pool.

    ./dashboard.sh init
    ./dashboard.sh scrape                 # network/cache only; never Telegram
    ./dashboard.sh deliver --live         # saved cache only; never scrapes
    ./dashboard.sh run                    # all configured sources (cached per day)
    ./dashboard.sh run --fixture listings.json
    ./dashboard.sh run --live
    ./dashboard.sh check
    ./dashboard.sh prune
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from uuid import uuid4

from chc_rental.event_store import AlertStoreError
from chc_rental.delivery_worker import DeliveryWorker
from chc_rental.fetch import fetch_many_daily, load_many_daily_cached, scrape_day
from chc_rental.incremental import IncrementalCollector
from chc_rental.notify.telegram import TelegramSendError, build_sender
from chc_rental.outbox import process_incremental_report
from chc_rental.operations import (
    ROLLOUT_ATTESTATIONS,
    incremental_cost_status,
    incremental_rollout_readiness,
)
from chc_rental.pipeline import (
    PipelineResult,
    deliver,
    deliver_source_outage_notices,
    plan_pushes,
    validate_records,
)
from chc_rental.scheduler import IncrementalScheduler
from chc_rental.sources import configured_adapters, enabled_cache_sources
from chc_rental.sources.apify import ApifyClient, ApifyRunState
from chc_rental.sources.base import SourceError
from chc_rental.sources.planner import plan_queries
from chc_rental.sources.zillow import ZillowRentalAdapter, load_apify_token
from chc_rental.store import Store, StoreError

FIXTURE_SOURCE = "fixture"


def _alert_operator(store: Store, env_file: str, text: str) -> bool:
    """Best-effort Telegram alert to the operator. Must never break the run.

    Independent of `live_push_enabled`: that flag gates pushes to recipients,
    while this is the operator channel that reports the system's own health.
    """
    try:
        settings = store.load_settings()
        if not settings.operator_alert_telegram_id:
            return False
        sender = build_sender(env_file)
        if sender is None:
            return False
        sender.send(
            telegram_id=settings.operator_alert_telegram_id,
            text=f"[chc-rental] {text}",
        )
        return True
    except Exception:
        return False


def _parse_now(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("--now must include a timezone offset (e.g. ...Z or +08:00)")
    return parsed.astimezone(timezone.utc)


def _load_fixture(path: Path) -> list[Any]:
    if not path.is_file():
        raise ValueError(f"fixture must be an existing file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"fixture is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("fixture must contain a JSON list of listing objects")
    return payload


def _cmd_init(args: argparse.Namespace) -> int:
    store = Store(args.root)
    store.initialize()
    print(f"initialized {store.root}")
    print(f"  allowlist: {store.allowlist_path}")
    print(f"  settings : {store.settings_path}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    store = Store(args.root)
    store.initialize()
    now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    with store.try_run_lock() as acquired:
        if not acquired:
            print("another CHC_Rental run is already in progress; nothing to do")
            return 0
        try:
            return _run_once(store, args, now_utc)
        except Exception as exc:
            # The daily job is unattended; a crash nobody sees is a silent outage.
            _alert_operator(
                store, getattr(args, "env_file", ".env"),
                f"daily run crashed: {type(exc).__name__}: {exc}",
            )
            raise


def _record_daily_run(
    store: Store,
    now_utc: datetime,
    summary: dict[str, Any],
    *,
    kind: str,
    rejected: int = 0,
    fetch: Optional[dict[str, Any]] = None,
) -> None:
    """Write one run record. ``kind`` is what lets alert dedup compare like with
    like: the split job emits a scrape record and a delivery record per tick."""
    payload: dict[str, Any] = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "summary": summary,
        "rejected": rejected,
    }
    if fetch is not None:
        payload["fetch"] = fetch
    store.record_run(now_utc, payload)


def _print_fetch_notes(pool) -> None:
    for note in pool.configuration_warnings:
        print(f"source configuration: {note}")
    for report in pool.sources:
        for note in report.warnings:
            print(f"note [{report.source}]: {note}")
        for error in report.errors:
            print(f"fetch error [{report.source}]: {error}")


def _pool_reason(pool, fallback: str = "no listing pool") -> str:
    notes = list(pool.configuration_warnings)
    for report in pool.sources:
        notes.extend(report.errors or report.warnings)
    return "; ".join(notes) or fallback


def _cmd_scrape(args: argparse.Namespace) -> int:
    """Update the daily source snapshot; never construct a Telegram sender."""
    store = Store(args.root)
    store.initialize()
    now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    with store.try_run_lock() as acquired:
        if not acquired:
            print("another CHC_Rental run is already in progress; scrape skipped")
            return 0

        settings = store.load_settings()
        adapters, configuration_warnings = configured_adapters(
            settings, env_path=args.env_file
        )
        if not adapters:
            reason = "no listing source configured"
            if configuration_warnings:
                reason += ": " + "; ".join(configuration_warnings)
            print(f"{reason}; scrape skipped")
            _record_daily_run(
                store,
                now_utc,
                {"scrape": "skipped", "reason": reason},
                kind="scrape",
            )
            return 1 if configuration_warnings else 0

        pool = fetch_many_daily(
            store,
            adapters,
            now_utc=now_utc,
            configuration_warnings=configuration_warnings,
        )
        _print_fetch_notes(pool)
        fetch_summary = pool.summary()
        if pool.usable:
            status = "fetched" if any(r.fetched for r in pool.sources) else "cached"
            summary = {
                "scrape": status,
                "scrape_day": scrape_day(settings, now_utc).isoformat(),
                "records": len(pool.records),
                "requests_used": sum(r.requests_used for r in pool.sources),
            }
            # Retention work now happens once, alongside the one paid scrape,
            # instead of on every ten-minute delivery check.
            if any(r.requests_used for r in pool.sources):
                store.prune(today=scrape_day(settings, now_utc), settings=settings)
            print("SCRAPE " + json.dumps(summary, sort_keys=True))
            _alert_on_scrape_problems(store, args.env_file, fetch_summary, summary)
            _record_daily_run(store, now_utc, summary, kind="scrape", fetch=fetch_summary)
            return 1 if any(r.errors for r in pool.sources) else 0

        reason = _pool_reason(pool)
        summary = {
            "scrape": "waiting" if not any(r.errors for r in pool.sources) else "failed",
            "scrape_day": scrape_day(settings, now_utc).isoformat(),
            "reason": reason,
        }
        print("SCRAPE " + json.dumps(summary, sort_keys=True))
        # The unattended job runs `scrape` and `deliver`, never `run`, so this is
        # the ONLY place a total fetch failure can be reported to the operator.
        _alert_on_scrape_problems(store, args.env_file, fetch_summary, summary)
        _record_daily_run(store, now_utc, summary, kind="scrape", fetch=fetch_summary)
        return 1 if any(r.errors for r in pool.sources) else 0


def _cmd_deliver(args: argparse.Namespace) -> int:
    """Plan/send only from today's saved pool; this path cannot call Apify."""
    store = Store(args.root)
    store.initialize()
    now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    with store.try_run_lock() as acquired:
        if not acquired:
            print("another CHC_Rental run is already in progress; delivery check skipped")
            return 0

        settings = store.load_settings()
        sources = enabled_cache_sources(settings)
        if not sources:
            reason = "no enabled saved-cache source"
            print(f"{reason}; delivery skipped")
            _record_daily_run(
                store, now_utc, {"delivery": "skipped", "reason": reason}, kind="delivery"
            )
            return 0

        pool = load_many_daily_cached(store, sources, now_utc=now_utc)
        _print_fetch_notes(pool)
        fetch_summary = pool.summary()
        if not pool.usable:
            reason = _pool_reason(pool, "no compatible current scrape-day cache")
            print(f"no usable saved listing pool ({reason}); delivery skipped")
            _alert_on_unusable_pool(store, args.env_file, reason)
            summary: dict[str, Any] = {"delivery": "skipped", "reason": reason}
            # Silence reads as "nothing matched today" to anyone who asked to
            # hear about empty days. When the scrape actually failed, say so.
            if _last_scrape_failed(store):
                outage = deliver_source_outage_notices(
                    store,
                    sender=build_sender(args.env_file) if args.live else None,
                    live=args.live,
                    now_utc=now_utc,
                )
                if outage["sent"]:
                    print(f"sent {outage['sent']} source-outage notice(s)")
                summary["source_outage_notices"] = outage
            _record_daily_run(
                store,
                now_utc,
                summary,
                kind="delivery",
                fetch=fetch_summary,
            )
            return 0

        day = scrape_day(settings, now_utc)
        listings, rejected = validate_records(
            store, pool.records, day=day, source="saved-daily-pool"
        )
        if rejected:
            print(
                f"note: {rejected} record(s) failed validation and were kept "
                "in state/rejected/"
            )
        result = plan_pushes(
            store, listings, now_utc=now_utc, allow_no_results=pool.complete
        )
        if not pool.complete:
            print("note: source coverage was incomplete; no-results notices are suppressed")

        sender = build_sender(args.env_file) if args.live else None
        if args.live and sender is None:
            print(
                "note: --live ignored. No usable TELEGRAM_BOT_TOKEN in "
                f"{args.env_file} (it is still 'changeme' or absent)."
            )
        result = deliver(
            store, result, sender=sender, live=args.live, now_utc=now_utc
        )
        print(result.preview())
        print("SUMMARY " + json.dumps(result.summary, sort_keys=True))
        _alert_on_problems(store, args.env_file, result, fetch_summary, kind="delivery")
        _record_daily_run(
            store,
            now_utc,
            result.summary,
            kind="delivery",
            rejected=rejected,
            fetch=fetch_summary,
        )
        if args.live and result.summary.get("delivery") != "live":
            print(
                "note: --live had no effect. Set live_push_enabled: true in "
                "config/settings.yaml and supply a sender."
            )
        return 0


def _run_once(store: Store, args: argparse.Namespace, now_utc: datetime) -> int:
    today = scrape_day(store.load_settings(), now_utc)

    def record(summary: dict[str, Any], *, rejected: int = 0, fetch: Optional[dict] = None) -> None:
        payload: dict[str, Any] = {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "kind": "run",
            "summary": summary,
            "rejected": rejected,
        }
        if fetch is not None:
            payload["fetch"] = fetch
        store.record_run(now_utc, payload)

    if args.fixture:
        raw = _load_fixture(Path(args.fixture))
        source = FIXTURE_SOURCE
        fetch_summary: dict[str, Any] = {"source": source, "fixture": str(args.fixture)}
        pool_complete = True
    else:
        settings = store.load_settings()
        adapters, configuration_warnings = configured_adapters(
            settings, env_path=args.env_file
        )
        if not adapters:
            reason = "no listing source configured"
            if configuration_warnings:
                reason += ": " + "; ".join(configuration_warnings)
            print(
                f"{reason}; nothing to do "
                f"(configure a source in {args.env_file} or use --fixture)"
            )
            record({"delivery": "skipped", "reason": reason})
            return 1 if configuration_warnings else 0

        pool = fetch_many_daily(
            store,
            adapters,
            now_utc=now_utc,
            configuration_warnings=configuration_warnings,
        )
        fetch_summary = pool.summary()
        for note in pool.configuration_warnings:
            print(f"source configuration: {note}")
        for report in pool.sources:
            for note in report.warnings:
                print(f"note [{report.source}]: {note}")
            for error in report.errors:
                print(f"fetch error [{report.source}]: {error}")
        if not pool.usable:
            # Either every source failed, or it is before scrape time / there
            # are no fetchable searches. Planning against an empty pool here
            # would turn an outage into a false "nothing matched" notification.
            notes = list(pool.configuration_warnings)
            for report in pool.sources:
                notes.extend(report.errors or report.warnings)
            reason = "; ".join(notes) or "no listing pool"
            print(f"no usable listing pool ({reason}); skipping plan/deliver")
            if any(report.errors for report in pool.sources) and not _same_failure_as_last_run(
                store, reason
            ):
                _alert_operator(
                    store,
                    args.env_file,
                    f"fetch failed, no pushes today: {reason}",
                )
            record({"delivery": "skipped", "reason": reason}, fetch=fetch_summary)
            return 1 if any(report.errors for report in pool.sources) else 0
        raw = pool.records
        source = "multi-source-pool"
        pool_complete = pool.complete

    listings, rejected = validate_records(store, raw, day=today, source=source)
    if rejected:
        print(f"note: {rejected} record(s) failed validation and were kept in state/rejected/")

    result = plan_pushes(
        store, listings, now_utc=now_utc, allow_no_results=pool_complete
    )
    if not pool_complete:
        print("note: source coverage was incomplete; no-results notices are suppressed")

    sender = build_sender(args.env_file) if args.live else None
    if args.live and sender is None:
        print(
            "note: --live ignored. No usable TELEGRAM_BOT_TOKEN in "
            f"{args.env_file} (it is still 'changeme' or absent)."
        )
    result = deliver(store, result, sender=sender, live=args.live, now_utc=now_utc)

    print(result.preview())
    print("SUMMARY " + json.dumps(result.summary, sort_keys=True))

    _alert_on_problems(store, args.env_file, result, fetch_summary)
    record(result.summary, rejected=rejected, fetch=fetch_summary)
    if args.live and result.summary.get("delivery") != "live":
        print(
            "note: --live had no effect. Set live_push_enabled: true in "
            "config/settings.yaml and supply a sender."
        )
    return 0


def _same_failure_as_last_run(store: Store, reason: str, *, kind: str = "run") -> bool:
    """True when the previous run OF THIS KIND already recorded this failure.

    Alert on the first occurrence of a failure streak, then stay quiet until
    something changes — frequent unattended checks must not repeatedly page
    the operator about one broken subscription. The kind filter is load-bearing:
    the scheduled job writes a scrape record and a delivery record every tick,
    so comparing against whichever landed last would flip the signature each
    time and defeat the dedup entirely.
    """
    try:
        last = store.latest_run_log(kind=kind) or {}
        return (last.get("summary") or {}).get("reason") == reason
    except Exception:
        return False


def _last_scrape_failed(store: Store) -> bool:
    """True when the most recent scrape reported an outright failure.

    The delivery command reads only saved caches, so on its own it cannot tell
    "the source was down" from "it is not scrape time yet" — both look like a
    missing cache. The scrape record is where that distinction lives.
    """
    summary = (store.latest_run_log(kind="scrape") or {}).get("summary") or {}
    return summary.get("scrape") == "failed"


def _alert_on_unusable_pool(store: Store, env_file: str, reason: str) -> None:
    """Page when delivery has no pool on a day whose scrape did not already page.

    Deliberately narrow. A failed scrape is reported by `_alert_on_scrape_problems`
    and must not page twice for one root cause; "waiting" is the normal state
    before scrape time; "skipped" is the operator's own kill switch. What is left
    — the scrape reported success yet delivery cannot use the result — is the
    genuinely new signal, and it is currently invisible.
    """
    last_scrape = (store.latest_run_log(kind="scrape") or {}).get("summary") or {}
    if last_scrape.get("scrape") in {"failed", "waiting", "skipped"}:
        return
    if _same_failure_as_last_run(store, reason, kind="delivery"):
        return
    _alert_operator(store, env_file, f"delivery had no usable listing pool: {reason}")


def _alert_on_scrape_problems(
    store: Store,
    env_file: str,
    fetch_summary: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    """Page the operator when the paid scrape itself failed.

    `_alert_on_problems` needs a PipelineResult and only runs once delivery has
    something to deliver, so it can never see a day where the fetch produced no
    pool at all. That is precisely the outage worth paging about — on 2026-08-20
    an exhausted Apify subscription went unreported for a full day — so this
    path reports it directly and dedups against the previous SCRAPE record only.
    """
    problems = _problem_messages(fetch_summary, {})
    if not problems:
        return
    reason = str(summary.get("reason") or "; ".join(problems))
    if _same_failure_as_last_run(store, reason, kind="scrape"):
        return
    _alert_operator(store, env_file, "scrape failed: " + "; ".join(problems))


def _alert_on_problems(
    store: Store,
    env_file: str,
    result: PipelineResult,
    fetch_summary: dict[str, Any],
    *,
    kind: str = "run",
) -> None:
    problems = _problem_messages(fetch_summary, result.summary)
    if not problems:
        return

    # A truncated day cache is replayed by frequent checks. Alert on the first
    # occurrence of a problem streak and whenever the signature changes, not
    # on every free cache replay. This runs before the current run is recorded,
    # so latest_run_log is the previous cycle.
    try:
        previous = store.latest_run_log(kind=kind) or {}
        previous_problems = _problem_messages(
            previous.get("fetch") or {}, previous.get("summary") or {}
        )
    except Exception:
        previous_problems = []
    if problems == previous_problems:
        return
    _alert_operator(store, env_file, " · ".join(problems))


def _problem_messages(
    fetch_summary: dict[str, Any], result_summary: dict[str, Any]
) -> list[str]:
    """Stable operator-problem signature shared by live and cached runs."""
    problems: list[str] = []
    source_summaries = fetch_summary.get("sources")
    if not isinstance(source_summaries, list):
        source_summaries = [fetch_summary]
    for source_summary in source_summaries:
        if not isinstance(source_summary, dict):
            continue
        source = str(source_summary.get("source", "source"))
        if source_summary.get("errors"):
            problems.append(
                f"{source} fetch errors: " + "; ".join(source_summary["errors"])
            )
        if source_summary.get("truncated"):
            problems.append(f"{source} fetch was truncated")
    if fetch_summary.get("configuration_warnings"):
        problems.append(
            "source configuration: " + "; ".join(fetch_summary["configuration_warnings"])
        )
    failed = result_summary.get("failed", 0)
    notices_failed = result_summary.get("no_results_failed", 0)
    if failed:
        problems.append(f"{failed} push(es) failed to send")
    if notices_failed:
        problems.append(f"{notices_failed} no-results notice(s) failed to send")
    return problems


def _cmd_check(args: argparse.Namespace) -> int:
    """Confirm the bot exists and can message each allowlisted person.

    A bot cannot open a conversation with someone who has never messaged it, so
    an unreachable recipient fails silently at push time unless it is checked.
    """
    store = Store(args.root)
    store.initialize()
    sender = build_sender(args.env_file)
    if sender is None:
        print(f"no usable TELEGRAM_BOT_TOKEN in {args.env_file}")
        return 1
    try:
        me = sender.whoami()
    except TelegramSendError as exc:
        print(f"Telegram bot authentication failed: {exc}")
        print(
            f"Replace TELEGRAM_BOT_TOKEN in {args.env_file} with the current "
            "token from @BotFather, then retry."
        )
        return 1
    print(f"bot: @{me.get('username')} (id {me.get('id')}, name {me.get('first_name')})")
    problems = 0
    for person in store.load_allowlist().people:
        state = "allowlisted" if person.active else "removed"
        if sender.can_reach(person.telegram_id):
            reach = "reachable"
        else:
            reach = "UNREACHABLE — they must message the bot first"
            problems += person.active
        searches = len(person.profile.active_searches())
        print(
            f"  {person.telegram_id} {person.display_name}: {state}, "
            f"{searches} active search(es), {reach}"
        )
    return 1 if problems else 0


def _cmd_usage(args: argparse.Namespace) -> int:
    """Report Apify monthly spend against the ceiling. Reads only; costs nothing.

    Hitting the ceiling stops the product dead — no scrape, no cache, no push —
    and the only warning the platform gives is the failure itself. This makes
    the number checkable before that happens. Exit 1 once spend is within 15%
    of the cap so it can be used as a scripted check, not just read by eye.
    """
    store = Store(args.root)
    store.initialize()
    token = load_apify_token(args.env_file)
    if token is None:
        print(f"no usable APIFY_TOKEN in {args.env_file}")
        return 1
    settings = store.load_settings()
    try:
        limits = ApifyClient(
            token=token, timeout=float(settings.zillow_timeout_seconds)
        ).account_limits()
    except SourceError as exc:
        print(f"could not read Apify usage: {exc}")
        return 1

    used = limits["monthly_usage_usd"]
    cap = limits["monthly_cap_usd"]
    print(f"apify cycle: {limits['cycle_start']} -> {limits['cycle_end']}")
    if used is None or cap is None or cap <= 0:
        print("apify usage: unavailable")
        return 1
    share = used / cap
    print(f"apify usage: ${used:.2f} / ${cap:.2f} ({share:.0%})")

    day = scrape_day(settings, datetime.now(timezone.utc))
    budget = settings.source_request_budget("zillow")
    queries, _ = plan_queries(store.load_allowlist())
    print(
        f"planned queries/day: {len(queries)} (zillow budget {budget}/day) — "
        f"one query is one paid actor run"
    )
    if len(queries) > budget:
        print(
            f"WARNING: {len(queries)} planned queries cannot fit a budget of "
            f"{budget}; the last {len(queries) - budget} are never fetched"
        )
        return 1
    if share >= 0.85:
        print("WARNING: within 15% of the Apify ceiling; scrapes will start failing")
        return 1
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    store = Store(args.root)
    store.initialize()
    settings = store.load_settings()
    removed = store.prune(
        today=scrape_day(settings, datetime.now(timezone.utc)), settings=settings
    )
    print("pruned " + json.dumps(removed, sort_keys=True))
    return 0


def _alert_foundation_status(store: Store) -> dict[str, Any]:
    return {
        "config": store.config_v2_status(),
        "ledger": store.alert_migration_status().as_dict(),
    }


def _alert_operational_status(store: Store, *, env_file: str) -> dict[str, Any]:
    payload = _alert_foundation_status(store)
    settings = store.load_settings()
    used = store.quota_used(
        scrape_day(settings, datetime.now(timezone.utc)), "zillow"
    )
    budget = settings.source_request_budget("zillow")
    token_ready = load_apify_token(env_file) is not None
    payload["incremental"] = {
        "enabled": settings.incremental_alerts_enabled,
        "zillow_enabled": settings.zillow_enabled,
        "token_ready": token_ready,
        "actor": settings.zillow_actor,
        "interval_minutes": settings.zillow_incremental_interval_minutes,
        "active_window": {
            "start": settings.incremental_active_start,
            "end": settings.incremental_active_end,
            "timezone": settings.scrape_timezone,
        },
        "daily_budget": budget,
        "used_today": used,
        "remaining_today": max(0, budget - used),
        "results_limit": settings.zillow_results_limit,
        "max_charge_usd": settings.zillow_max_charge_usd,
        "canary_ids": settings.incremental_canary_telegram_ids,
        "health": (
            store.event_store().health_snapshot()
            if payload["ledger"]["ready"]
            else None
        ),
    }
    payload["incremental"]["cost"] = (
        incremental_cost_status(
            store, settings, now_utc=datetime.now(timezone.utc)
        )
        if payload["ledger"]["ready"]
        else None
    )
    payload["incremental"]["rollout"] = incremental_rollout_readiness(
        store,
        settings,
        now_utc=datetime.now(timezone.utc),
        token_ready=token_ready,
        telegram_ready=build_sender(env_file) is not None,
    )
    return payload


def _cmd_alerts_readiness(args: argparse.Namespace) -> int:
    """Report rollout blockers without changing a gate or contacting a service."""
    store = Store(args.root)
    store.initialize()
    settings = store.load_settings()
    env_file = args.env_file or str(store.root / ".env")
    now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    payload = incremental_rollout_readiness(
        store,
        settings,
        now_utc=now_utc,
        token_ready=load_apify_token(env_file) is not None,
        telegram_ready=build_sender(env_file) is not None,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "first canary: "
            f"{'READY' if payload['ready_for_first_canary'] else 'BLOCKED'}; "
            "recipient expansion: "
            f"{'READY' if payload['ready_for_recipient_expansion'] else 'BLOCKED'}"
        )
        for item in payload["checks"]:
            marker = "ok" if item["ok"] else "BLOCK"
            print(f"  [{marker}] {item['id']}: {item['detail']}")
    return 0 if payload["ready_for_first_canary"] else 1


def _cmd_alerts_attest(args: argparse.Namespace) -> int:
    """Record or clear one explicit owner-reviewed rollout gate."""
    store = Store(args.root)
    store.initialize()
    if not store.alert_migration_status().ready:
        raise ValueError("alert-ledger migration is required before rollout attestation")
    if args.confirm_gate != args.gate:
        raise ValueError("--confirm-gate must exactly match --gate")
    now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    events = store.event_store()
    if args.clear:
        changed = events.clear_rollout_attestation(args.gate, now_utc=now_utc)
        payload = {"gate": args.gate, "cleared": changed}
    else:
        if not args.evidence or not args.evidence.strip():
            raise ValueError("--evidence is required when recording an attestation")
        saved = events.attest_rollout_gate(
            args.gate, evidence=args.evidence, now_utc=now_utc
        )
        payload = {
            "gate": saved.gate,
            "evidence": saved.evidence,
            "attested_at": saved.attested_at,
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cmd_alerts_migrate(args: argparse.Namespace) -> int:
    """Inspect or apply the inert incremental-alert foundation."""
    store = Store(args.root)
    store.initialize()
    if args.apply:
        config = store.migrate_config_v2()
        ledger = store.migrate_alert_ledger().as_dict()
        payload = {"config": config, "ledger": ledger}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if config["ready"] and ledger["ready"] else 1
    print(json.dumps(_alert_foundation_status(store), indent=2, sort_keys=True))
    return 0


def _cmd_alerts_status(args: argparse.Namespace) -> int:
    """Read-only foundation status; never creates the alert database."""
    store = Store(args.root)
    store.initialize()
    env_file = args.env_file or str(store.root / ".env")
    payload = _alert_operational_status(store, env_file=env_file)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        config = payload["config"]
        ledger = payload["ledger"]
        print(
            "incremental foundation: "
            f"config={'ready' if config['ready'] else 'migration needed'}, "
            f"ledger={'ready' if ledger['ready'] else 'migration needed'}; "
            f"collection={'on' if payload['incremental']['enabled'] else 'paused'}, "
            f"Zillow={'on' if payload['incremental']['zillow_enabled'] else 'off'}"
        )
    return 0


class _FixtureApifyClient:
    """Offline actor-shaped transport used only by ``alerts cycle --fixture``."""

    def __init__(self, payload: list[Any]) -> None:
        self.payload = payload

    def start_actor(self, actor, payload, *, max_total_charge_usd):
        run_id = f"fixture-{uuid4()}"
        return ApifyRunState(run_id, "SUCCEEDED", run_id, 0.0, "offline fixture")

    def get_run(self, run_id):  # pragma: no cover - fixtures finish at start
        return ApifyRunState(run_id, "SUCCEEDED", run_id, 0.0, "offline fixture")

    def get_dataset(self, dataset_id):
        return self.payload


def _cmd_alerts_cycle(args: argparse.Namespace) -> int:
    """Run one source-only incremental cycle; never sends Telegram."""
    store = Store(args.root)
    store.initialize()
    now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    settings = store.load_settings()
    if args.fixture:
        if store.alert_db_path.exists() and store.event_store().open_source_runs():
            raise ValueError("fixture cycle refuses to impersonate an existing open live source run")
        raw = _load_fixture(Path(args.fixture))
        client = _FixtureApifyClient(raw)
        adapter = ZillowRentalAdapter(
            token="fixture",
            results_limit=max(25, len(raw) + 1),
            bounds_resolver=lambda query: {
                "west": -180.0,
                "east": 180.0,
                "south": -85.0,
                "north": 85.0,
            },
        )
        allow_disabled = True
        meter_requests = False
    else:
        token = load_apify_token(args.env_file)
        if not token:
            raise ValueError(f"no usable APIFY_TOKEN in {args.env_file}")
        client = ApifyClient(token=token, timeout=settings.zillow_timeout_seconds)
        adapter = ZillowRentalAdapter(
            token=token,
            actor=settings.zillow_actor,
            results_limit=settings.zillow_results_limit,
            timeout=settings.zillow_timeout_seconds,
            max_charge_usd=settings.zillow_max_charge_usd,
        )
        allow_disabled = False
        meter_requests = True
    with store.try_run_lock() as acquired:
        if not acquired:
            print(json.dumps({"skipped_locked": True}, sort_keys=True))
            return 0
        report = IncrementalCollector(
            store,
            settings=settings,
            adapter=adapter,
            client=client,
            allow_disabled=allow_disabled,
            meter_requests=meter_requests,
        ).cycle(now_utc=now_utc)
        processing = process_incremental_report(store, report, now_utc=now_utc)
    summary = report.summary()
    summary["processing"] = processing.summary()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not any(item.error for item in report.collections) else 1


def _cmd_alerts_tick(args: argparse.Namespace) -> int:
    """Run one locked unattended scheduler tick or an offline fixture tick."""
    store = Store(args.root)
    store.initialize()
    if not store.config_v2_status()["ready"]:
        raise ValueError("config migration is required before an incremental tick")
    if not store.alert_migration_status().ready:
        raise ValueError("alert-ledger migration is required before an incremental tick")
    if not args.live and not args.fixture:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "note": "no source, outbox, backup, or Telegram state changed",
                },
                sort_keys=True,
            )
        )
        return 0
    if args.max_messages <= 0:
        raise ValueError("--max-messages must be positive")
    now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    settings = store.load_settings()
    env_file = args.env_file or str(store.root / ".env")
    collector = None
    sender = None
    source_reason = None
    if args.fixture:
        raw = _load_fixture(Path(args.fixture))
        collector = IncrementalCollector(
            store,
            settings=settings,
            adapter=ZillowRentalAdapter(
                token="fixture",
                results_limit=max(25, len(raw) + 1),
                bounds_resolver=lambda query: {
                    "west": -180.0,
                    "east": 180.0,
                    "south": -85.0,
                    "north": 85.0,
                },
            ),
            client=_FixtureApifyClient(raw),
            allow_disabled=True,
            meter_requests=False,
        )
    else:
        token = load_apify_token(env_file)
        if token:
            collector = IncrementalCollector(
                store,
                settings=settings,
                adapter=ZillowRentalAdapter(
                    token=token,
                    actor=settings.zillow_actor,
                    results_limit=settings.zillow_results_limit,
                    timeout=settings.zillow_timeout_seconds,
                    max_charge_usd=settings.zillow_max_charge_usd,
                ),
                client=ApifyClient(
                    token=token, timeout=settings.zillow_timeout_seconds
                ),
            )
        else:
            source_reason = "APIFY_TOKEN is missing; no source request was attempted"
        sender = build_sender(env_file)
    report = IncrementalScheduler(
        store,
        collector=collector,
        sender=sender,
        source_unavailable_reason=source_reason,
        operator_alert=(
            None
            if args.fixture
            else lambda text: _alert_operator(store, env_file, text)
        ),
        runtime_mode="fixture" if args.fixture else "live",
    ).tick(now_utc=now_utc, max_delivery_messages=args.max_messages)
    print(json.dumps(report.summary(), indent=2, sort_keys=True))
    return 1 if report.warnings and not report.skipped_locked else 0


def _cmd_alerts_backup(args: argparse.Namespace) -> int:
    """Create/verify a backup or perform a disposable restore drill."""
    store = Store(args.root)
    store.initialize()
    if not store.alert_migration_status().ready:
        raise ValueError("alert-ledger migration is required before backup operations")
    events = store.event_store()
    if args.verify:
        payload = events.verify_backup(args.verify)
    elif args.drill:
        payload = events.restore_drill(args.drill)
    else:
        now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
        with store.try_run_lock() as acquired:
            if not acquired:
                raise ValueError("another rental workflow is already running")
            path = events.create_daily_backup(now_utc=now_utc)
            payload = {
                "backup": str(path),
                "verification": events.verify_backup(path),
                "restore_drill": events.restore_drill(path),
            }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cmd_alerts_deliver(args: argparse.Namespace) -> int:
    """Inspect or explicitly run the canary-only incremental outbox worker."""
    store = Store(args.root)
    store.initialize()
    if not store.alert_migration_status().ready:
        raise ValueError("alert-ledger migration is required before delivery")
    if not args.live:
        payload = {
            "mode": "dry-run",
            "outbox": store.event_store().outbox_counts(),
            "note": "no outbox state changed and no Telegram call was made",
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.confirm_telegram_id is None:
        raise ValueError("--live requires --confirm-telegram-id for one configured canary")
    if args.confirm_telegram_id <= 0:
        raise ValueError("--confirm-telegram-id must be positive")
    if args.max_messages <= 0:
        raise ValueError("--max-messages must be positive")
    if args.outbox_id is not None:
        selected = next(
            (
                row
                for row in store.event_store().outbox_records()
                if row.outbox_id == args.outbox_id
            ),
            None,
        )
        if selected is None:
            raise ValueError(f"unknown outbox row: {args.outbox_id}")
        if selected.telegram_id != args.confirm_telegram_id:
            raise ValueError("the selected outbox row belongs to a different recipient")
        if selected.status in {"sent", "uncertain", "cancelled", "failed"}:
            raise ValueError(
                f"outbox {selected.outbox_id} cannot be auto-delivered from status "
                f"{selected.status}"
            )
    sender = build_sender(args.env_file)
    if sender is None:
        raise ValueError(f"no usable TELEGRAM_BOT_TOKEN in {args.env_file}")
    now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    with store.try_run_lock() as acquired:
        if not acquired:
            raise ValueError("another rental workflow is already running")
        report = DeliveryWorker(store, sender=sender).run(
            now_utc=now_utc,
            telegram_ids={args.confirm_telegram_id},
            outbox_id=args.outbox_id,
            max_messages=args.max_messages,
        )
    # Never follow an ambiguous recipient send with another automatic Telegram
    # call: the first message may already have been accepted. Definite failures
    # may alert a distinct operator; if Peter is both recipient and operator, the
    # dashboard remains the safe review channel for his own failed canary.
    terminal = [
        row
        for row in store.event_store().terminal_failures_needing_alert()
        if row.status == "failed"
    ]
    operator_id = store.load_settings().operator_alert_telegram_id
    if terminal and operator_id != args.confirm_telegram_id:
        text = "incremental delivery needs review: " + ", ".join(
            f"outbox {row.outbox_id}={row.status}" for row in terminal[:10]
        )
        if _alert_operator(store, args.env_file, text):
            store.event_store().mark_terminal_failure_alerted(
                [row.outbox_id for row in terminal], now_utc=now_utc
            )
    print(json.dumps(report.summary(), indent=2, sort_keys=True))
    return 1 if report.failed or report.uncertain or report.retry_wait else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dashboard.sh", description="CHC Rental daily runner."
    )
    parser.add_argument("--root", default=".", help="Data directory (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create config/ and state/ with defaults").set_defaults(
        func=_cmd_init
    )

    run_parser = sub.add_parser("run", help="Plan (and optionally send) today's push")
    run_parser.add_argument(
        "--fixture",
        help="JSON file holding listing objects; overrides all network sources. "
        "Without it and without an enabled credentialed source, the run does "
        "nothing rather than invent listings.",
    )
    run_parser.add_argument("--now", help="Override the current instant (ISO 8601 with offset)")
    run_parser.add_argument(
        "--live", action="store_true", help="Attempt real delivery instead of a dry run"
    )
    run_parser.add_argument(
        "--env-file", default=".env", help="Dotenv holding TELEGRAM_BOT_TOKEN (default: %(default)s)"
    )
    run_parser.set_defaults(func=_cmd_run)

    scrape_parser = sub.add_parser(
        "scrape", help="Update today's source cache without any Telegram delivery"
    )
    scrape_parser.add_argument(
        "--now", help="Override the current instant (ISO 8601 with offset)"
    )
    scrape_parser.add_argument("--env-file", default=".env")
    scrape_parser.set_defaults(func=_cmd_scrape)

    deliver_daily_parser = sub.add_parser(
        "deliver", help="Plan/send from today's saved cache without any source fetch"
    )
    deliver_daily_parser.add_argument(
        "--now", help="Override the current instant (ISO 8601 with offset)"
    )
    deliver_daily_parser.add_argument("--live", action="store_true")
    deliver_daily_parser.add_argument("--env-file", default=".env")
    deliver_daily_parser.set_defaults(func=_cmd_deliver)

    check_parser = sub.add_parser("check", help="Verify the bot can reach every allowlisted person")
    check_parser.add_argument("--env-file", default=".env")
    check_parser.set_defaults(func=_cmd_check)

    usage_parser = sub.add_parser(
        "usage", help="Show Apify monthly spend against its subscription ceiling"
    )
    usage_parser.add_argument("--env-file", default=".env")
    usage_parser.set_defaults(func=_cmd_usage)

    sub.add_parser("prune", help="Delete state past its retention window").set_defaults(
        func=_cmd_prune
    )

    alerts_parser = sub.add_parser(
        "alerts", help="Incremental-alert migration and read-only status"
    )
    alerts_sub = alerts_parser.add_subparsers(dest="alerts_command", required=True)
    migrate_parser = alerts_sub.add_parser(
        "migrate", help="Inspect or apply config/ledger migrations"
    )
    migrate_mode = migrate_parser.add_mutually_exclusive_group()
    migrate_mode.add_argument(
        "--apply",
        action="store_true",
        help="Back up config and apply pending incremental-alert migrations",
    )
    migrate_mode.add_argument(
        "--check",
        action="store_false",
        dest="apply",
        help="Inspect pending migrations without changing config or state (default)",
    )
    migrate_parser.set_defaults(apply=False)
    migrate_parser.set_defaults(func=_cmd_alerts_migrate)
    alerts_status_parser = alerts_sub.add_parser(
        "status", help="Show incremental foundation readiness without changing it"
    )
    alerts_status_parser.add_argument("--json", action="store_true")
    alerts_status_parser.add_argument(
        "--env-file", help="Credential file for readiness checks (default: ROOT/.env)"
    )
    alerts_status_parser.set_defaults(func=_cmd_alerts_status)
    readiness_parser = alerts_sub.add_parser(
        "readiness", help="Show first-canary and recipient-expansion blockers"
    )
    readiness_parser.add_argument("--json", action="store_true")
    readiness_parser.add_argument("--now", help="Override instant (ISO 8601 with offset)")
    readiness_parser.add_argument(
        "--env-file", help="Credential file for readiness checks (default: ROOT/.env)"
    )
    readiness_parser.set_defaults(func=_cmd_alerts_readiness)
    attest_parser = alerts_sub.add_parser(
        "attest", help="Record or clear one explicitly reviewed rollout gate"
    )
    attest_parser.add_argument("--gate", required=True, choices=ROLLOUT_ATTESTATIONS)
    attest_parser.add_argument("--evidence", help="Short, non-secret review evidence")
    attest_parser.add_argument("--clear", action="store_true")
    attest_parser.add_argument(
        "--confirm-gate",
        required=True,
        help="Must exactly repeat --gate to confirm the local state change",
    )
    attest_parser.add_argument("--now", help="Override instant (ISO 8601 with offset)")
    attest_parser.set_defaults(func=_cmd_alerts_attest)
    cycle_parser = alerts_sub.add_parser(
        "cycle", help="Run one source-only incremental shadow cycle"
    )
    cycle_mode = cycle_parser.add_mutually_exclusive_group(required=True)
    cycle_mode.add_argument("--fixture", help="Offline JSON list of raw Zillow actor items")
    cycle_mode.add_argument(
        "--shadow", action="store_true", help="Use the enabled live Zillow source; never send"
    )
    cycle_parser.add_argument("--now", help="Override the current instant (ISO 8601 with offset)")
    cycle_parser.add_argument("--env-file", default=".env")
    cycle_parser.set_defaults(func=_cmd_alerts_cycle)
    deliver_parser = alerts_sub.add_parser(
        "deliver", help="Inspect or explicitly deliver canary outbox rows"
    )
    deliver_parser.add_argument("--live", action="store_true")
    deliver_parser.add_argument(
        "--confirm-telegram-id",
        type=int,
        help="Explicit configured canary recipient required with --live",
    )
    deliver_parser.add_argument(
        "--outbox-id", type=int, help="Promote only this shadow row before delivery"
    )
    deliver_parser.add_argument("--max-messages", type=int, default=1)
    deliver_parser.add_argument("--now", help="Override instant (ISO 8601 with offset)")
    deliver_parser.add_argument("--env-file", default=".env")
    deliver_parser.set_defaults(func=_cmd_alerts_deliver)
    tick_parser = alerts_sub.add_parser(
        "tick", help="Run one locked incremental scheduler tick"
    )
    tick_mode = tick_parser.add_mutually_exclusive_group()
    tick_mode.add_argument(
        "--live", action="store_true", help="Use gated live Apify and Telegram transports"
    )
    tick_mode.add_argument("--fixture", help="Offline Zillow actor JSON fixture")
    tick_parser.add_argument("--now", help="Override instant (ISO 8601 with offset)")
    tick_parser.add_argument("--max-messages", type=int, default=10)
    tick_parser.add_argument(
        "--env-file", help="Credential file (default: ROOT/.env)"
    )
    tick_parser.set_defaults(func=_cmd_alerts_tick)
    backup_parser = alerts_sub.add_parser(
        "backup", help="Create, verify, or drill an incremental-ledger backup"
    )
    backup_mode = backup_parser.add_mutually_exclusive_group()
    backup_mode.add_argument("--verify", help="Verify an existing SQLite backup")
    backup_mode.add_argument(
        "--drill", help="Restore an existing backup into a disposable database and verify it"
    )
    backup_parser.add_argument("--now", help="Override instant for daily backup naming")
    backup_parser.set_defaults(func=_cmd_alerts_backup)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, StoreError, AlertStoreError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
