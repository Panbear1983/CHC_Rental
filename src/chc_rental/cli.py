"""Command-line entry point for the daily run.

Listings come from every enabled source adapter (RentCast plus optional Zillow)
or from a local JSON fixture for offline runs and tests. Fetching is separately
day-cached and budget-capped per source in `chc_rental.fetch`; everything
downstream (validate, match, dedupe, cap, deliver) consumes the combined pool.

    chc-rental init
    chc-rental run                        # all configured sources (cached per day)
    chc-rental run --fixture listings.json
    chc-rental run --live
    chc-rental check
    chc-rental prune
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from uuid import uuid4

from chc_rental.event_store import AlertStoreError
from chc_rental.fetch import fetch_many_daily
from chc_rental.incremental import IncrementalCollector
from chc_rental.notify.telegram import build_sender
from chc_rental.outbox import process_incremental_report
from chc_rental.pipeline import PipelineResult, deliver, plan_pushes, validate_records
from chc_rental.sources import configured_adapters
from chc_rental.sources.apify import ApifyClient, ApifyRunState
from chc_rental.sources.zillow import ZillowRentalAdapter, load_apify_token
from chc_rental.store import Store, StoreError

FIXTURE_SOURCE = "fixture"


def _alert_owner(store: Store, env_file: str, text: str) -> None:
    """Best-effort Telegram alert to the operator. Must never break the run.

    Independent of `live_push_enabled`: that flag gates pushes to recipients,
    while this is the operator channel that reports the system's own health.
    """
    try:
        settings = store.load_settings()
        if not settings.owner_telegram_id:
            return
        sender = build_sender(env_file)
        if sender is None:
            return
        sender.send(telegram_id=settings.owner_telegram_id, text=f"[chc-rental] {text}")
    except Exception:
        pass


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
            _alert_owner(
                store, getattr(args, "env_file", ".env"),
                f"daily run crashed: {type(exc).__name__}: {exc}",
            )
            raise


def _run_once(store: Store, args: argparse.Namespace, now_utc: datetime) -> int:
    today = now_utc.date()

    def record(summary: dict[str, Any], *, rejected: int = 0, fetch: Optional[dict] = None) -> None:
        payload: dict[str, Any] = {
            "finished_at": datetime.now(timezone.utc).isoformat(),
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
                _alert_owner(store, args.env_file, f"fetch failed, no pushes today: {reason}")
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


def _same_failure_as_last_run(store: Store, reason: str) -> bool:
    """True when the most recent run already recorded this exact failure.

    Alert on the first occurrence of a failure streak, then stay quiet until
    something changes — an unattended hourly job must not page the operator
    24 times about one broken subscription.
    """
    try:
        last = store.latest_run_log() or {}
        return (last.get("summary") or {}).get("reason") == reason
    except Exception:
        return False


def _alert_on_problems(
    store: Store, env_file: str, result: PipelineResult, fetch_summary: dict[str, Any]
) -> None:
    problems = _problem_messages(fetch_summary, result.summary)
    if not problems:
        return

    # A truncated day cache is replayed hourly by design. Alert on the first
    # occurrence of a problem streak and whenever the signature changes, not
    # on every free cache replay. This runs before the current run is recorded,
    # so latest_run_log is the previous cycle.
    try:
        previous = store.latest_run_log() or {}
        previous_problems = _problem_messages(
            previous.get("fetch") or {}, previous.get("summary") or {}
        )
    except Exception:
        previous_problems = []
    if problems == previous_problems:
        return
    _alert_owner(store, env_file, " · ".join(problems))


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
    me = sender.whoami()
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


def _cmd_prune(args: argparse.Namespace) -> int:
    store = Store(args.root)
    store.initialize()
    removed = store.prune(today=date.today(), settings=store.load_settings())
    print("pruned " + json.dumps(removed, sort_keys=True))
    return 0


def _alert_foundation_status(store: Store) -> dict[str, Any]:
    return {
        "config": store.config_v2_status(),
        "ledger": store.alert_migration_status().as_dict(),
    }


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
    payload = _alert_foundation_status(store)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        config = payload["config"]
        ledger = payload["ledger"]
        print(
            "incremental foundation: "
            f"config={'ready' if config['ready'] else 'migration needed'}, "
            f"ledger={'ready' if ledger['ready'] else 'migration needed'}"
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="chc-rental", description="CHC Rental daily runner.")
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

    check_parser = sub.add_parser("check", help="Verify the bot can reach every allowlisted person")
    check_parser.add_argument("--env-file", default=".env")
    check_parser.set_defaults(func=_cmd_check)

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
    alerts_status_parser.set_defaults(func=_cmd_alerts_status)
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

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, StoreError, AlertStoreError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
