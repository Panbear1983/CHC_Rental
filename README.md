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
| 0 — source vetting and run policy | **decided 2026-08-11** — US only; RentCast licensed, Zillow managed route owner-approved/flagged |
| 1 — file store and config schema | **built** |
| 2 — matching, dedup, seen ledger | **built** — source-independent key v3 reads legacy v2 ledgers safely |
| 3 — source adapters and fetch budget | **built** — resilient RentCast + opt-in Zillow pool, per-source cache/budget |
| 4 — TUI dashboard | **built** — saved-filter refresh plus per-source status/quota/cache visibility |
| 5 — Telegram push | **built** — @Panbear_Buddy_bot, outbound only |
| 6 — daily automation | **built** — hourly launchd job via tmux, daily prune, operator alerts |

`chc-rental run` combines every usable configured source into one daily pool.
RentCast activates when `RENTCAST_API_KEY` is usable. Zillow requires both an
`APIFY_TOKEN` and the explicit `zillow_enabled: true` setting; the token alone
can never activate the flagged source. Each source has its own query-aware day
cache and request ceiling: adding/removing a watched city invalidates that
source cache, while local price/bed/filter edits reuse the city pool. One source
failing does not discard positive matches from the other, but no-results
notices are suppressed while coverage is incomplete.

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
- `dedup.py` — stable cross-source identity with legacy seen-ledger compatibility.
- `notification_schedule.py` — per-person local-time due calculation.
- `pipeline.py` — validate → allowlist → due → match → dedupe → cap → send.
- `sources/` — RentCast and opt-in Zillow rental adapters plus the `(city,
  state)` query planner that dedupes fetches across everyone's searches.
- `fetch.py` — daily fetch orchestration: scrape-time gate, query-aware raw
  cache, per-request budget metering, bounded 429/transient retry.
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

# Or use the terminal wrapper (resolves the venv + root for you, and controls
# the hourly launchd job):
./chc.sh                 # launch the dashboard
./chc.sh run --live      # run today's push now
./chc.sh check           # bot reachability per person
./chc.sh job status      # is the hourly job loaded?
./chc.sh job stop|start  # unload / load it
./chc.sh logs -f         # follow the daily job log
./chc.sh help
```

Manage the allowlist, each person's searches, their delivery time and timezone,
and view a read-only status screen (per-source readiness, quota, cache and last
result, plus the latest delivery run and rejected records).

The per-person preference table shows location, property types, price, bed/bath,
square-footage and feature filters. Saving, editing, toggling or deleting a
preference immediately refreshes both that table and the search count on the
parent Telegram allowlist menu.

## Optional Zillow rental source

Zillow is disabled by default. After reviewing `docs/SOURCES.md`, add the token
to `.env` and opt in through `config/settings.yaml`:

```yaml
zillow_enabled: true
zillow_actor: maxcopell~zillow-scraper
zillow_results_limit: 25
zillow_timeout_seconds: 300
zillow_max_charge_usd: 0.25
source_daily_request_budgets:
  zillow: 5
```

`zillow_results_limit` caps results per city/actor run; the request budget caps
actor runs per UTC day; `zillow_max_charge_usd` is sent as the actor-run charge
ceiling. Set `zillow_enabled: false` or its request budget to `0` for an
immediate kill switch.

Before each paid city run, the adapter resolves that US city/state to the map
bounds required by the pinned actor through OpenStreetMap Nominatim. Actor
error markers and non-unit building summary cards are ignored; exact listing
cards then enter the same validation, matching and cross-source dedup pipeline
as RentCast.

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
nothing, rather than inventing listings. A whole-run lock also prevents a slow
managed scrape from overlapping a scheduled or manually-started cycle.
