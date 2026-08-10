# CHC_Rental

A daily rental-listing scraper with a terminal dashboard. Each allowlisted person
has one profile holding several independent saved searches; the daily run matches
new listings against every search and pushes the **links** to that person on
Telegram.

There is no database. Configuration and state are plain files, which makes both
easy to read, back up and hand-edit.

Plan: `.hermes/plans/2026-08-10_210000-chc-rental-filebased-scraper.md`.

## Architecture note: one-way notification, no inbound poller

This codebase does not run a Telegram poller, webhook, or any inbound message
handler. The architecture is **one-way outbound notification only**: the owner
manages allowlisted people and their searches locally, and the daily run pushes
qualifying listings out to already-allowlisted recipients. There is no user
self-enrollment path and no code that reads inbound Telegram messages or user
interaction of any kind.

## Build status

| Phase | State |
|---|---|
| 0 — source vetting and run policy | **not started** — needs the site list |
| 1 — file store and config schema | **built** |
| 2 — matching, dedup, seen ledger | **built** |
| 3 — source adapters and fetch budget | **not built** — gated on Phase 0 |
| 4 — TUI dashboard | **built** |
| 5 — Telegram push | **built** — @Panbear_Buddy_bot, outbound only |
| 6 — daily automation | **built** — hourly launchd job via tmux |

Until Phase 3 lands, listings come from a local JSON fixture. Everything
downstream of the fetch is real.

## Layout

```
config/        allowlist.yaml, settings.yaml     (gitignored: holds real Telegram IDs)
state/         seen/, quota/, runs/, rejected/, cache/
backups/       timestamped copy taken before every config write
examples/      allowlist.example.yaml, listings.sample.json
```

Every read and write of those paths goes through `chc_rental.store.Store`, which
holds an exclusive lock across each read-modify-write and replaces files
atomically. Nothing else may open them directly — a single enforcement point is
what keeps the allowlist rule from being bypassed.

## Modules

- `models.py` — `Search`, `Profile`, `AllowlistEntry`, `Allowlist`, `Settings`,
  `Listing`. Every business rule lives here.
- `store.py` — the single gate: locking, atomic writes, backups, seen ledger,
  request quota, run logs, rejected records, retention.
- `matching.py` — deterministic listing-to-search eligibility.
- `dedup.py` — stable per-listing identity, including city and district.
- `notification_schedule.py` — per-person local-time due calculation.
- `pipeline.py` — validate → allowlist → due → match → dedupe → cap → send.
- `cli.py` — `chc-rental init | run | prune`.
- `tui/` — the owner dashboard.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Owner dashboard

```bash
.venv/bin/chc-rental-tui                 # uses ./config and ./state
.venv/bin/chc-rental-tui --root /path/to/data
```

Manage the allowlist, each person's searches, their delivery time and timezone,
and view a read-only status screen (quota used today, last run, rejected records).

## Daily run

```bash
.venv/bin/chc-rental init
.venv/bin/chc-rental check          # bot identity + can it reach each person
.venv/bin/chc-rental run --fixture examples/listings.sample.json
.venv/bin/chc-rental run --fixture examples/listings.sample.json --now 2026-01-15T05:00:00Z
.venv/bin/chc-rental prune
```

The run is a dry run unless `--live` is passed **and** `live_push_enabled: true`
is set in `config/settings.yaml` **and** a sender is wired in (Phase 5). A person
is only pushed to once per local calendar day, after their `delivery_time`.

## Enforced rules

- A person receives listings only while they are on the allowlist and active;
  membership is re-checked at plan time and again immediately before each send.
- Each search caps how many listings it contributes per day.
- A person receives any given listing at most once, ever — across runs and across
  their own overlapping searches.
- A listing is marked as delivered only after a confirmed send, so a failed send
  stays retryable.
- A record that fails validation is written to `state/rejected/` and the run
  continues; one bad record never discards a good batch.
- Listings must carry an `http(s)` link, since the link is the payload.
- Search names are unique per profile; Telegram IDs are unique across the file.

## Migration from the SQLite build

```bash
python scripts/migrate_from_sqlite.py data/chc_rental.sqlite3 --root .
```

Each old preference profile becomes one search under its owner's profile. Run it
once, check the output, then delete the script.

## Run tests

```bash
pytest -q
```

## Scheduled runs

`scripts/com.chcrental.daily.plist` fires hourly and lets each person's own
local delivery time decide whether they are due — a fixed clock time would need
a UTC offset that breaks at every DST transition and cannot serve two people in
different timezones.

The command is wrapped in `tmux` deliberately: a launchd job invoking the repo
venv directly is denied by TCC because the repo lives under `~/Desktop`, failing
with `PermissionError` on `pyvenv.cfg`. tmux already holds that grant.

```bash
cp scripts/com.chcrental.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.chcrental.daily.plist
```

With no source configured the run logs "no listing source configured" and does
nothing, rather than inventing listings.

## Not built yet

- **No source adapter, so nothing is fetched.** Gated on Phase 0 vetting. The
  RentCast key in `.env` returns HTTP 403 `billing/subscription-inactive`, so
  that route needs an active subscription before it can be used.
