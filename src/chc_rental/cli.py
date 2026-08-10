"""Command-line entry point for the daily run.

Until Phase 3 supplies vetted source adapters, listings come from a local JSON
fixture. The pipeline, dedup, capping and delivery gating are all real, so the
only thing that changes when a source lands is where the records come from.

    chc-rental init
    chc-rental run --fixture listings.json
    chc-rental run --fixture listings.json --live
    chc-rental prune
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from chc_rental.notify.telegram import build_sender
from chc_rental.pipeline import deliver, plan_pushes, validate_records
from chc_rental.store import Store, StoreError

FIXTURE_SOURCE = "fixture"


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
    today = now_utc.date()

    if not args.fixture:
        # No source adapter exists yet, so an unattended run has nothing to
        # fetch. Doing nothing is the only safe outcome — inventing listings
        # would push fabricated data to real people.
        print("no listing source configured; nothing to do (see Phase 0/3 in the plan)")
        store.write_run_log(
            today,
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "summary": {"delivery": "skipped", "reason": "no listing source configured"},
            },
        )
        return 0

    raw = _load_fixture(Path(args.fixture))
    listings, rejected = validate_records(store, raw, day=today, source=FIXTURE_SOURCE)
    if rejected:
        print(f"note: {rejected} record(s) failed validation and were kept in state/rejected/")

    result = plan_pushes(store, listings, now_utc=now_utc)

    sender = build_sender(args.env_file) if args.live else None
    if args.live and sender is None:
        print(
            "note: --live ignored. No usable TELEGRAM_BOT_TOKEN in "
            f"{args.env_file} (it is still 'changeme' or absent)."
        )
    result = deliver(store, result, sender=sender, live=args.live)

    print(result.preview())
    print("SUMMARY " + json.dumps(result.summary, sort_keys=True))

    store.write_run_log(
        today,
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary": result.summary,
            "rejected": rejected,
        },
    )
    if args.live and result.summary.get("delivery") != "live":
        print(
            "note: --live had no effect. Set live_push_enabled: true in "
            "config/settings.yaml and supply a sender (Phase 5)."
        )
    return 0


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
        help="JSON file holding listing objects. Omit once a source adapter exists; "
        "with neither, the run does nothing rather than invent listings.",
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

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, StoreError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
