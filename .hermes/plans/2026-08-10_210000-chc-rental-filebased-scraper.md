# CHC_Rental — File-Based Daily Scraper Buildout Plan

Supersedes `2026-08-07_060000-chc-rental-foundation.md`. Written 2026-08-10 after
Peter restated the premise.

## Premise (Peter, 2026-08-10)

> "this is a daily scraper of with a terminal user interface dashboard that wires
> to the profile based preferences. There is no database required for this
> buildout. Each profile should have multiple different settings of preferences
> that lets the scraper to push to user telegram on the allow list to receive
> rental related links."

Decisions taken with that message:

| Question | Answer |
|---|---|
| Sources | **Official APIs + open feeds** — official APIs, public RSS/JSON feeds, and any site whose robots.txt and terms explicitly allow automated access |
| Profile shape | **Profile holds many searches** — one person, one profile, N independent saved searches |
| Existing code | **Replace in place** inside CHC_Rental |

## What changes from the current build

Out: SQLite and everything built on it — `db.py`, `repositories.py`, `budgets.py`,
`delivery.py`, `db_recovery.py`, and the `preference_profiles` / `delivery_ledger`
/ `daily_budget_ledger` / `audit_events` tables.

In: a config-file + state-file store, a source-adapter layer, and a real daily
push. The nesting changes from `user -> many profiles (each = one search)` to
`user -> one profile -> many searches`.

Kept as-is (the 2026-08-10 audit verified these are sound): `notification_schedule.py`
— strongest module in the repo, one fire per local day across DST in four zones —
and `outbound.py`'s renderer, which is genuinely inert and recipient-free.
Kept with fixes: `matching.py` (extend to `Search`), `dedup.py` (see Phase 2),
`tui/` (rebuild on the store, reuse the 2026-08-09 layout fixes).

**"No database" does not mean "no persistence."** Deduplicating across daily runs
requires remembering what was already pushed. That state moves from SQLite to
plain files — which is simpler to inspect and back up, but reintroduces the same
concurrency hazards in a new form. Hence `store.py` below.

## Architecture

```
config/                        # human- and TUI-edited, YAML
  allowlist.yaml               #   people -> profile -> searches
  sources.yaml                 #   vetted sources + per-source limits
  settings.yaml                #   run time, timezone, budgets, kill switch
state/                         # machine-written, JSON/JSONL
  seen/<telegram_id>.jsonl     #   append-only: listing key, search, sent_at
  quota/<YYYY-MM-DD>.json      #   requests used today, per source
  runs/<YYYY-MM-DD>.json       #   run log: fetched/matched/pushed/failed
  rejected/<YYYY-MM-DD>.jsonl  #   records that failed validation, kept for review
  cache/<YYYY-MM-DD>/          #   raw source responses, so re-runs don't re-fetch
backups/                       # timestamped copy taken before every config write
src/chc_rental/
  store.py                     # THE single gate for all reads/writes
  models.py                    # Search, Profile, AllowlistEntry, Listing
  matching.py  dedup.py  notification_schedule.py
  sources/base.py  sources/<name>.py
  pipeline.py                  # fetch -> validate -> match -> dedupe -> push
  notify/telegram.py           # outbound only
  tui/                         # dashboard
  cli.py                       # chc-rental run [--dry-run|--live]
```

**One gate, not two.** Every read and write of config or state goes through
`store.py`. The single worst structural flaw in the current build is that
`repositories.py` enforces the allowlist while `dry_run.py` and `db_recovery.py`
reach past it with raw SQL — which is how removed users stayed in the pipeline.
Nothing else may open these files directly.

## Data shape

```yaml
# config/allowlist.yaml
schema_version: 1
people:
  - telegram_id: 12345
    display_name: Peter
    active: true
    profile:
      delivery_time: "09:00"
      timezone: Asia/Taipei
      notify_on_no_results: false
      searches:
        - name: Downtown 2BR
          active: true
          city: Taipei
          district: Da'an
          price_min: 20000
          price_max: 35000
          beds_min: 2
          property_types: [apartment, condo]
          required_features: [elevator]
          excluded_features: [basement]
          daily_cap: 5
```

---

## Phase 0 — Source vetting and run policy (no code)

**Objective:** decide what may be fetched, and how often, before any fetcher exists.

1. Peter names candidate sources. For each, record in `docs/SOURCES.md`: the
   robots.txt verdict, the relevant terms clause, an evidence URL, the date
   checked, and a status of ALLOWED / ALLOWED-WITH-LIMITS / REFUSED.
2. Set in `config/settings.yaml`: daily run time + timezone, global daily request
   ceiling, per-source ceiling, per-search daily push cap, seen-ledger retention,
   kill-switch owner.
3. Carry forward the existing exclusion list — Zillow, Realtor.com, Craigslist,
   Facebook Marketplace, Trulia, Zumper, PadMapper, Apartments.com remain REFUSED.

**Exit:** at least one ALLOWED source with evidence recorded. No adapter may be
written in Phase 3 for a source not marked ALLOWED here.

## Phase 1 — File store and config schema (offline)

**Objective:** replace SQLite with one safe store.

- `models.py`: `Search`, `Profile`, `AllowlistEntry` (Pydantic, with the range and
  feature-overlap validators carried over from today's models).
- `store.py`: `fcntl.flock` around every operation, atomic writes via temp file +
  `os.replace`, a timestamped backup into `backups/` before each config write,
  a `schema_version` field, and validation on load with a clear error naming the
  offending path.
- `migrate_from_sqlite.py`: one-shot export of the existing SQLite rows into
  `config/allowlist.yaml`, mapping each old profile to a `Search` under its
  owner's profile. Run once, verify counts, then delete the script.
- Delete: `db.py`, `repositories.py`, `budgets.py`, `delivery.py`,
  `db_recovery.py`, `status.py` and their tests.

**Exit:** round-trip test (load → edit → save → load) preserves every field; a
concurrent-writer test proves no lost update; migration reproduces the current
row counts exactly.

## Phase 2 — Matching, dedup, seen-ledger (offline, fixtures)

**Objective:** decide correctly what each search *would* push, with no network and
no sending.

- Extend `matching.py` to operate on `Search`.
- **Rewrite the dedup key.** Today's fallback is
  `addr:{source}:{address}:{unit}` — no city — so `100 Main St, Austin` and
  `100 Main St, Dallas` collide and one subscriber is silently skipped. The new
  key includes source, city, district, address and unit, escapes its separators,
  and keeps a stable identity whether or not the source returns an id (today the
  key shape flips, re-notifying everyone).
- Seen ledger: append-only JSONL per telegram_id, `is_new(telegram_id, key)`.
- **Intra-run dedup:** a person receives a listing at most once per run even when
  several of their searches match it; attribute it to the first matching search.
- `pipeline.py --dry-run` over a fixture directory. A record that fails validation
  is written to `state/rejected/` and the run continues — one bad record must
  never abort the batch.

**Exit:** tests cover the four seam cases the audit found — cross-city collision,
id-appears-later, one person with two overlapping searches, one malformed record
in a good batch.

## Phase 3 — Source adapters and fetch budget (first network)

**Objective:** fetch real listings from one vetted source, under a hard ceiling.

- `sources/base.py`: `SourceAdapter` protocol, `fetch(search) -> FetchResult`.
- One adapter for the top-ranked ALLOWED source. Pagination handled; results
  filtered to currently-active listings where the source exposes status.
- Quota: date-stamped file, incremented atomically under lock, checked *before*
  each request; a hard stop plus kill switch. Honor `Retry-After` and 429 with
  backoff. Distinguish auth failure from rate limit from server error.
- Raw responses cached per day so a re-run costs nothing.
- Any credential field declared `repr=False`; never logged, never in a traceback.

**Exit:** a `--dry-run` against a live fetch shows real listings; the quota
decrements; exceeding the ceiling refuses cleanly; a simulated 429 backs off.

## Phase 4 — TUI dashboard

**Objective:** the operator control plane, reflecting real state.

- Screens: allowlist (add / deactivate), profile → searches (add / edit / delete /
  toggle active), run status (last run, next run, quota remaining, per-search
  counts, rejected records), and a dry-run preview.
- Reuse the 2026-08-09 layout fixes: `DataTable { height: 1fr }`, `height: auto`
  on dialog rows, labels at `width: 100%`, Save/Cancel pinned below a scrolling
  field area.
- The TUI must drive **and** reflect the real scheduler — no automation it cannot
  show or control.

**Exit:** headless pilot test per screen; nothing clipped at 80x24; deactivating a
person does not crash any screen (today it kills the dashboard).

## Phase 5 — Telegram outbound push

**Objective:** deliver links, outbound only.

- Reuse `outbound.py`'s renderer; add `notify/telegram.py` as the real sender.
  No poller, no webhook, no inbound handler — unchanged from day one.
- Push order, strictly: re-check allowlist membership → confirm not already seen →
  send → **only then** append to the seen ledger. Never mark seen before a
  confirmed send.
- Bounded retries. A sent item is terminal, with no path back to pending —
  today's `mark_failed` resurrects sent rows and allows three duplicate sends.
- `--dry-run` is the default; `--live` must be explicit. Global kill switch.

**Exit:** a live send to Peter's own Telegram id; an immediate second run sends
nothing.

## Phase 6 — Daily automation and ops

**Objective:** unattended daily runs the TUI can see and control.

- launchd job at the configured local time.
  **Known hazard:** launchd jobs running from `~/Desktop` hit TCC exit 78 with
  framework Python (repo venvs included); the tmux-chain and `uv` python
  launchers were previously granted. Verify empirically before calling this done.
- Run log per day plus a failure alert to Telegram.
- Retention: prune seen ledger, cache, rejects and backups per the Phase 0 policy.
- TUI `autostart on|off` driving the launchd job, mirroring the pattern that works
  in Alpaca_Paper_Trader.

**Exit:** two consecutive unattended daily runs with correct pushes and zero
duplicates.

---

## Defects not to re-inherit

From the 2026-08-10 audit of the current build:

1. Two data-access paths with enforcement on only one — hence one `store.py`.
2. Dedup key missing city/district, and key shape flipping with id presence.
3. No intra-run dedup across one person's searches.
4. One malformed record aborting the whole batch, with nothing retained.
5. A "sent" record that can be flipped back and re-sent.
6. Non-atomic read-modify-write on the budget counter, which lost updates and
   silently un-tripped the circuit breaker. The file quota must be lock-guarded.
7. Secrets in dataclass `repr`, reaching tracebacks and `pytest --showlocals`.
8. Safety mechanisms with zero test coverage — the quota and seen-ledger get
   tests in the same phase that introduces them, not later.

## Open items for Peter

- Which sources to vet in Phase 0 (Taiwan portals, US regional, or both).
- Which Telegram bot to use: a new one, or an existing bot from the four already
  mapped. `.env` currently holds a real `RENTCAST_API_KEY` and a placeholder
  `TELEGRAM_BOT_TOKEN`.
- Whether RentCast stays a candidate source (it is an official API and qualifies,
  but costs per call and its adapter needs the `repr=False` fix first).
- Coordinate with whoever else is editing this repo before Phase 1 deletes
  modules — commit or branch first so there is a restore point.
