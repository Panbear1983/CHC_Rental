# CHC_Rental

A rental-listing notifier with a terminal dashboard. The preserved daily path
combines configured sources into a digest; the disabled-by-default incremental
path observes bounded Zillow/Apify query windows and can alert a controlled
Telegram canary after a silent baseline.

Human-controlled configuration remains plain YAML. Incremental run recovery,
baselines and notification receipts use a private SQLite operational ledger;
it is not a property database and has no historical property-search UI.

Current plan: `.hermes/plans/2026-08-12_120000-chc-rental-incremental-apify-alerts.md`.

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
| 0 — source vetting and run policy | **decided 2026-08-11; updated 2026-08-13** — US only; **Zillow via Apify is the sole source** (RentCast removed) |
| 1 — file store and config schema | **built** |
| 2 — matching, dedup, seen ledger | **built** — source-independent key v3 reads legacy v2 ledgers safely |
| 3 — source adapters and fetch budget | **built** — Zillow/Apify adapter, filter-aware query planner, per-source cache/budget |
| 4 — TUI dashboard | **built** — saved-filter refresh plus per-source status/quota/cache visibility |
| 5 — Telegram push | **built** — @Panbear_Buddy_bot, outbound only |
| 6 — daily automation | **built** — 10-minute launchd checks via tmux; scrape-only and delivery-only paths, daily prune, operator alerts |
| Incremental 0–6 | **built, paused** — durable Apify runs, silent baselines, shadow outbox, canary delivery, dashboard, breaker/cost/backup operations |
| Incremental 7 | **built, not activated** — readiness evidence and audited gates exist; controlled Peter message and 48-hour canary remain |

The scheduled job calls `./dashboard.sh scrape` to update the daily Zillow pool
and then `./dashboard.sh deliver --live` to consume only that saved pool. The
two commands cannot cross responsibilities: scrape never sends Telegram, and
deliver never constructs a source adapter or calls Apify. `./dashboard.sh run`
remains a combined manual compatibility command. Zillow requires both an
`APIFY_TOKEN` and the explicit
`zillow_enabled: true` setting; the token alone can never activate the flagged
source. The source has its own query-aware day
cache and request ceiling: adding/removing a watched city invalidates that
source cache, while local price/bed/filter edits reuse the city pool. One source
failing does not discard positive matches from the other, but no-results
notices are suppressed while coverage is incomplete.

## Layout

```
config/        allowlist.yaml, settings.yaml     (gitignored: holds real Telegram IDs)
state/         seen/, delivery-journal/, quota/, runs/, rejected/, cache/, alerts.sqlite3
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
  routine delivery journal, request quota, run logs, rejected records, retention.
- `matching.py` — deterministic listing-to-search eligibility.
- `dedup.py` — stable cross-source identity with legacy seen-ledger compatibility.
- `notification_schedule.py` — per-person local-time due calculation.
- `pipeline.py` — validate → allowlist → due → match → dedupe → cap → send.
- `sources/` — the Zillow/Apify rental adapter plus the `(city,
  state)` query planner that dedupes fetches across everyone's searches.
- `fetch.py` — daily fetch orchestration: scrape-time gate, query-aware raw
  cache, per-request budget metering, bounded 429/transient retry.
- `cli.py` — internal implementation for `./dashboard.sh init | scrape | deliver | run | prune`.
- `incremental.py`, `events.py`, `outbox.py`, `delivery_worker.py` — durable
  source runs, observation classification and canary-only delivery.
- `scheduler.py`, `operations.py` — locked ticks, breaker/cost policy and health.
- `tui/` — the owner dashboard.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Owner dashboard

`dashboard.sh` is the sole public entry point. It resolves the virtual
environment and repository root automatically and also controls the background
jobs:

```bash
./dashboard.sh                 # launch the dashboard
./dashboard.sh scrape          # source/cache only; never Telegram
./dashboard.sh deliver --live  # saved cache only; never Apify
./dashboard.sh run --live      # run today's push now
./dashboard.sh check           # bot reachability per person
./dashboard.sh test            # run the suite; `job start` refuses while it is red
./dashboard.sh usage           # Apify monthly spend vs its subscription ceiling
./dashboard.sh job status      # is the 10-minute job loaded?
./dashboard.sh job stop|start  # unload / load it
./dashboard.sh logs -f         # follow the daily job log
./dashboard.sh alerts status   # incremental gates, health, cost and queues
./dashboard.sh alerts readiness --json # first-canary and expansion blockers
./dashboard.sh alerts-job status # separate job; not loaded by default
./dashboard.sh help
```

Manage the allowlist, each person's searches, their delivery mode/timezone, and
view source readiness, quota, cache, health and the latest delivery run. The
additive Alerts screen exposes real persisted gates, query health, canaries and
outbox actions.

Selecting a member and pressing `Edit` opens a full member-details page. Its
`Routine diary` tab groups scheduled Telegram pushes by the recipient's local
date and preserves the exact outbound content, listing facts, direct URL, and
Telegram receipt for one year. Accepted no-results notices are included.
Interrupted sends are shown as `UNCERTAIN` and are never claimed as delivered
or retried automatically. Existing scheduled seen-ledger rows are imported on
a best-effort basis and visibly marked `LEGACY` when the original message facts
were not retained. New Test Push and immediate-alert messages do not appear
automatically. Explicitly recovered historical Test Push evidence may be shown
with a `TEST PUSH · LEGACY` label and never affects scheduled deduplication.

The main-page `Test push` action sends the selected active recipient the newest
matching cards from the latest compatible Zillow cache. Every card uses the
same compact address/price/bed/bath format as the daily push and includes its
direct Zillow link. All active saved searches are evaluated, overlapping
results are sent once with every matching preference named, and the action
performs no paid Zillow fetch. Each Telegram-ID has its own 90-day delivery
bank shared by scheduled, incremental, and Test Push delivery. A successful
Test Push therefore suppresses those same properties for that recipient, but
does not advance their next scheduled push time. If every compatible match was
already sent, the preview changes to an explicit red repeat confirmation; that
repeat is audited without restarting the original 90-day clock. New searches
default to a 25-listing safety limit, matching the current per-query Zillow
result limit. Before
anything is sent, the dashboard shows a scrollable plain-text preview of every
Telegram part and direct link; confirmation sends that exact prepared payload.
A retained cache may cover extra cities, but Test push accepts it only when it
contains every city required by the selected recipient.

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
actor runs per configured scrape-timezone day; `zillow_max_charge_usd` is sent
as the actor-run charge
ceiling. Set `zillow_enabled: false` or its request budget to `0` for an
immediate kill switch.

**The query count is the bill.** The planner emits exactly one query — one paid
actor run — per watched (city, state), no matter how many people watch it or how
wide their filters are. Widening or subdividing a city's price/bed span is free;
adding a query is not. A planner change on 2026-08-16 split one city into six
sub-queries, took Zillow spend from 1 to 5 runs/day, and exhausted the Apify
monthly subscription on 2026-08-20 — which stopped every recipient's push.
`tests/test_sources.py::test_query_count_never_exceeds_the_number_of_watched_cities`
guards this.

Local budgets cannot see the Apify subscription's own monthly ceiling; reaching
it fails every run with HTTP 403 `platform-feature-disabled`. `./dashboard.sh usage`
reports spend against that ceiling and exits non-zero within 15% of it.

Before each paid city run, the adapter resolves that US city/state to the map
bounds required by the pinned actor through OpenStreetMap Nominatim. Actor
error markers and non-unit building summary cards are ignored; exact listing
cards then enter the same validation, matching and cross-source dedup pipeline
as Zillow.

## Daily run

```bash
./dashboard.sh init
./dashboard.sh check          # bot identity + can it reach each person
./dashboard.sh run --fixture examples/listings.sample.json
./dashboard.sh run --fixture examples/listings.sample.json --now 2026-01-15T05:00:00Z
./dashboard.sh prune
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

`scripts/com.chcrental.daily.plist` fires at `:00, :10, ... :50` each hour. It first runs the
scrape-only command, whose New York calendar-day cache permits at most one
normal fetch at/after the configured scrape time, and then runs delivery-only
against that cache. Each person's local delivery time decides whether they are
due. Missed ticks catch up after the Mac wakes; a fixed UTC clock would break at
DST transitions and could not serve people in different timezones.

The command is wrapped in `tmux` deliberately: a launchd job invoking the repo
venv directly is denied by TCC because the repo lives under `~/Desktop`, failing
with `PermissionError` on `pyvenv.cfg`. tmux already holds that grant.

```bash
cp scripts/com.chcrental.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.chcrental.daily.plist
```

With no source configured the scrape logs "no listing source configured" and
does nothing rather than inventing listings. With no compatible current
scrape-day cache, delivery skips; it never falls back to a network call or a
stale prior-day pool. A whole-run lock prevents a slow managed scrape from
overlapping a scheduled or manually-started cycle.

The incremental job is a separate disabled plist at
`scripts/com.chcrental.alerts.plist`; it is not installed or loaded as part of
the build. See [docs/INCREMENTAL_OPERATIONS.md](docs/INCREMENTAL_OPERATIONS.md)
for shadow validation, one-row canary delivery, backups, activation and rollback.
See [docs/SOURCE_ONBOARDING.md](docs/SOURCE_ONBOARDING.md) before considering
another Apify-backed site.
