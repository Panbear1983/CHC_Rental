# CHC_Rental — Incremental Apify Rental Alert Architecture Plan

Written 2026-08-12 after Peter reframed the product from a daily rental-data
digest into a passive, preference-matched alert service for newly observed
rental listings.

This plan is the implementation contract for the next buildout. It does **not**
alter the currently working daily workflow. It supersedes the fetch and delivery
parts of `2026-08-10_210000-chc-rental-filebased-scraper.md` only after the
incremental path passes the Peter-only canary and an explicit cutover is made.

Implementation status (2026-08-12): phases 0–7 are built behind disabled gates.
The external exit gates are deliberately still open: no live source/canary call
or LaunchAgent activation was performed by the build, and the 48-hour/two-day
observation evidence must be earned in operation.

## Outcome

Build a near-real-time, one-way Telegram alert pipeline that:

- collects bounded Zillow rental results through the already integrated,
  owner-approved Apify actor;
- shares identical source queries across allowlisted users;
- determines locally and deterministically which saved searches match;
- silently establishes a first-run baseline;
- alerts only on genuinely new observations after that baseline;
- persists notification intent before attempting Telegram delivery;
- never activates a source merely because a credential exists;
- keeps the current daily workflow available as the rollback path.

RentCast remains installed during migration. It is not the primary incremental
source because its current rental endpoint lacks consumer-facing listing links,
amenities and useful neighborhood coverage. It can be disabled after Zillow
passes the canary, but it is not deleted in this buildout.

Facebook Marketplace and Craigslist are **not** included in this implementation.
Their current terms do not inherit the narrow Zillow/Apify approval. Each future
source must pass the Phase 7 onboarding gate separately.

## Verified starting point

The repository currently has:

- owner-controlled `allowlist.yaml`, one profile per Telegram ID and multiple
  saved searches per profile;
- deterministic Pydantic validation and matching;
- source-independent deduplication with legacy-ledger compatibility;
- a RentCast adapter and an opt-in Zillow/Apify adapter;
- query sharing at the `(city, state)` level;
- atomic file writes, locks, quotas, raw day caches and rejection records;
- one-way Telegram delivery that marks a listing seen only after send success;
- a working owner TUI and source-status screen;
- an hourly launchd/tmux wrapper around a daily scrape and delivery gate;
- 166 passing tests as of this plan's creation.

The current worktree contains the completed but uncommitted daily/Zillow work.
Implementation must begin with a recoverable checkpoint and must not mix that
checkpoint with the incremental architecture changes.

## Product decisions made by this plan

| Question | Decision |
|---|---|
| Initial incremental source | Zillow rentals through the pinned Apify actor only |
| Source status | Owner-approved and terms-flagged; disabled by default |
| RentCast | Retain adapter; daily fallback/control source; disable only after canary |
| First observation | Silent baseline; no historical-result flood |
| Meaning of new | First observed by CHC after that source/query baseline |
| Eligibility | Existing deterministic matcher only; no LLM qualification |
| User enrollment | Existing owner-managed allowlist only; no Telegram inbound path |
| Config authority | YAML through `Store`; no secrets in YAML or SQLite |
| Runtime persistence | SQLite operational ledger, not a property-search database |
| Initial cadence | Existing 5 Zillow runs/day ceiling; scheduler spaces them across the configured active window |
| Faster cadence | Blocked until observed cost/run and a monthly budget are approved |
| Initial notification event | New listing only; price/repost events recorded but not sent |
| Cutover | Peter-only canary before any other allowlisted user |

The existing five-run Zillow ceiling cannot honestly provide a 30-minute
freshness target. With one active query scope, five runs can be spaced roughly
every three hours across a 15-hour active window; multiple query scopes share
those five starts and therefore have a longer effective interval. The runtime
must report actual freshness and coverage debt, never label that as real time.
After the canary measures actor cost and duration, Peter can approve a larger
monthly budget and shorter interval.

## Non-negotiable invariants

1. `chc-rental run` retains its current daily behavior until cutover.
2. New incremental code runs in fixture or shadow mode first.
3. `incremental_alerts_enabled: false` is the default and global kill switch.
4. A per-source `enabled` flag is required in addition to its credential.
5. A per-user delivery mode defaults to `daily`; migration cannot silently turn
   existing recipients into immediate-alert recipients.
6. The allowlist is rechecked both while enqueueing and immediately before send.
7. The first usable run for a source/query creates a baseline and queues nothing.
8. An incomplete, truncated or failed source run may produce positive new-item
   evidence but may never produce an absence/no-results conclusion.
9. Each external actor start reserves budget before the request.
10. One source failure cannot erase or invalidate observations from another.
11. No module outside the persistence boundary opens config/state storage directly.
12. The TUI may expose only controls that change real persisted runtime behavior.

## Target architecture

```text
allowlist.yaml + settings.yaml
              |
              v
       source query planner
    (stable query fingerprints)
              |
              v
        incremental scheduler
      (due, budget, run lease)
              |
              v
       source-specific runner
        (Apify run + dataset)
              |
              v
    validate + canonicalize records
              |
              v
 operational event ledger (SQLite)
  observation -> identity -> version
              |
              v
 existing deterministic preference matcher
              |
              v
       per-user durable outbox
              |
              v
   quiet hours / caps / allowlist check
              |
              v
        Telegram sender + receipt
```

### State ownership

Human-controlled configuration remains YAML:

```text
config/allowlist.yaml       people, profiles, searches, delivery policy
config/settings.yaml        workflow/source policy, cadence, budgets, kill switches
.env                        Telegram and Apify secrets only
```

Machine-controlled operational state moves into one SQLite file:

```text
state/alerts.sqlite3
```

This database is not a user-facing property database. It is the transactional
ledger required to know what was observed, what event it produced, which user
matched it, and whether Telegram accepted the notification. Raw source payloads
remain bounded and retention-controlled; there is no search UI or historical
property analytics built on this file.

The existing JSON/JSONL state remains readable during migration. The new ledger
is initially additive and does not rewrite or delete old seen, quota, cache or
run files.

### Operational tables

The first migration creates these logical tables:

- `schema_migrations`: ordered migration version and applied timestamp.
- `query_scopes`: stable source/query fingerprint, normalized query JSON,
  baseline state, last due/success time and next due time.
- `source_runs`: source, query scope, Apify run ID, status, timestamps, result
  count, truncation/coverage, error class and charge when available.
- `listing_identities`: cross-source identity key and first/last observed time.
- `listing_versions`: identity key, material version hash, canonical JSON,
  source provenance, source-posted time and CHC observation time.
- `observations`: one source run seeing one listing version.
- `outbox`: unique idempotency key, Telegram ID, stable search ID, listing
  identity/version, event type, not-before time, status and retry metadata.
- `delivery_receipts`: Telegram result metadata and final sent timestamp.

SQLite configuration: WAL mode, foreign keys on, busy timeout, explicit
transactions, migration backup and `PRAGMA integrity_check` after migration.
Only `Store` exposes public persistence operations; a private event-ledger
component may own the SQLite implementation behind that boundary.

### Stable identifiers

Every saved search needs an immutable `search_id`. Search names are editable and
cannot safely identify outbox messages. The schema-v2 config migration assigns
each existing search one ID, saves it once through `Store`, and preserves it on
all subsequent edits.

Notification idempotency key:

```text
telegram_id + search_id + listing_identity + event_type + material_version
```

The existing dedup identity remains the starting point. A separate material
version hash uses only fields whose change matters: price, rental status,
address/unit identity and preferred listing URL. Fetch timestamps, image order
and actor metadata must not create new versions.

### Event semantics

- `baseline`: visible during the first usable source/query run; stored, never sent.
- `new`: identity not previously observed after the baseline; eligible for send.
- `price_change`: same identity with a different price; stored only in MVP.
- `material_change`: relevant canonical fields changed; stored only in MVP.
- `repost_candidate`: listing returns after a defined absence or new source ID;
  stored for later policy work, not sent in MVP.
- `duplicate_source`: same identity appears through another source; provenance is
  added and the best direct link may replace a fallback, but no second alert.

“New” means new to CHC's observations, not a guarantee about the publisher's
actual posting time. Telegram messages and the dashboard must use honest wording.

## Phase summary

| Phase | Purpose | Current daily path affected? |
|---|---|---|
| 0 | Checkpoint, policy and fixture freeze | No |
| 1 | Config v2 and operational ledger in shadow mode | No |
| 2 | Incremental Apify runner and source-aware query scopes | No |
| 3 | Observation classification, matching and shadow outbox | No |
| 4 | Dashboard control and observability | Existing menus preserved; additive UI changes |
| 5 | Peter-only live delivery and controlled cutover | Only explicit canary profile |
| 6 | Scheduler, recovery, cost and operations hardening | New job added; daily job remains rollback |
| 7 | Broader rollout and future-source gate | Explicit owner activation only |

---

## Phase 0 — Recoverable checkpoint and frozen contracts

**Objective:** protect the working daily/Zillow implementation and freeze the
observable contracts before architecture changes.

**Actions:**

1. Review the current dirty diff and create one recoverable checkpoint for the
   completed daily/Zillow build. Do not fold incremental work into it.
2. Record `166 passed` as the baseline and save representative fixture payloads
   for RentCast, Zillow unit cards, Zillow building-summary cards, actor error
   markers, duplicates and malformed results.
3. Update `docs/DECISIONS.md` and `docs/SOURCES.md` with the scope in this plan:
   Zillow only for the first incremental source; Facebook/Craigslist unchanged.
4. Add no runtime settings yet. The phase is documentation, fixtures and source
   contract tests only.

**Files:**

- Modify: `docs/DECISIONS.md`, `docs/SOURCES.md`
- Add: `tests/fixtures/sources/zillow/*.json`
- Add: `tests/fixtures/sources/rentcast/*.json`
- Modify: `tests/test_sources.py`

**Tests:**

- Every supported Zillow payload maps deterministically or is rejected for the
  exact expected reason.
- Actor control records and building summaries never become listings.
- No credential is present in a fixture or tracked diff.
- Full current suite remains green.

**Exit gate:** clean/recoverable daily baseline, source decision recorded and
fixtures covering the payload seen in the live Peter test.

**Rollback:** not applicable; no runtime behavior changes.

## Phase 1 — Config schema v2 and operational ledger

**Objective:** introduce durable incremental state without changing fetching or
delivery.

**Model changes:**

- Add immutable `Search.search_id`.
- Add `Profile.delivery_mode: daily | immediate`, default `daily`.
- Add optional `quiet_hours_start` and `quiet_hours_end`; both or neither must be
  present and use the profile timezone.
- Preserve `delivery_time` for daily mode.
- Add global incremental settings with safe defaults:
  `incremental_alerts_enabled: false`, active window, scheduler tick and retention.
- Generalize source runtime policy while reading the current flat Zillow fields
  during the transition. Saving schema v2 writes one canonical representation.

**Implementation:**

- Add a one-time YAML migration under the existing store lock. It must back up
  both config files before assigning search IDs or changing schema version.
- Add `src/chc_rental/event_store.py` as a private implementation owned through
  `Store`; no pipeline/controller code receives a raw SQLite connection.
- Add ordered SQL migrations under `src/chc_rental/migrations/`.
- Add `chc-rental alerts migrate --check` and `--apply`; `--check` is read-only.
- In shadow mode, opening the dashboard or running the legacy command must not
  require the new database to exist.

**Files:**

- Modify: `src/chc_rental/models.py`, `src/chc_rental/store.py`
- Add: `src/chc_rental/event_store.py`
- Add: `src/chc_rental/migrations/0001_alert_ledger.sql`
- Modify: `src/chc_rental/cli.py`
- Modify: `config/settings.yaml`, `examples/allowlist.example.yaml`
- Add: `tests/test_config_v2_migration.py`, `tests/test_event_store.py`

**Tests:**

- Existing searches receive unique stable IDs exactly once.
- Editing/renaming a search preserves its ID.
- Failed migration restores the YAML backup and leaves the old daily path usable.
- Two processes cannot enqueue the same idempotency key.
- WAL reopen after simulated interruption passes integrity check.
- No secret or full Telegram payload is written into source-run error text.

**Exit gate:** schema v2 round trip, ledger migrations and concurrency tests pass;
legacy `chc-rental run` output is byte-equivalent for fixed fixtures.

**Rollback:** set schema-v2-only fields aside and restore the automatic config
backup; the new SQLite file can be moved out of `state/` without affecting daily
files.

## Phase 2 — Incremental runner and source-aware query planning

**Objective:** collect bounded source observations on a repeatable cadence while
delivery remains disabled.

The current daily `fetch.py` stays intact. Add a separate incremental path until
cutover.

**Query planning:**

- Compile each active saved search into a source-supported query envelope.
- For Zillow, push down city/state, price, beds, baths and supported property-type
  filters so the actor's result cap is not wasted on locally rejected records.
- The local matcher remains authoritative.
- Identical normalized envelopes across people share one query fingerprint and
  one actor run.
- Do not prematurely merge incompatible price/filter envelopes. Cost saving may
  not create coverage holes.
- A query that hits its result limit is marked truncated and proves only the
  positive listings returned.

**Apify run lifecycle:**

- Reserve the daily/global budget before actor start.
- Start a run and persist its Apify run ID before waiting for completion.
- Resume/poll an existing run after process restart rather than starting another.
- Retrieve the dataset only after terminal success.
- Classify auth, rate-limit, timeout, actor failure, malformed dataset and local
  validation failures separately.
- Record charge/usage when Apify exposes it; otherwise record `unknown`, never 0.
- Token stays in `.env` and is sent through the authorization mechanism supported
  by the endpoint; it is redacted from all URLs/logs/reprs.

**Scheduling:**

- A short local scheduler tick finds due query scopes; query scopes themselves
  have the slower budgeted interval.
- Missed intervals during sleep collapse into one catch-up run, not a burst of
  backfilled paid runs.
- Fairness is oldest-due scope first, with at most one new actor start per scope
  in one scheduler cycle.
- Initial Zillow maximum remains five starts per UTC day until cost approval.

**Files:**

- Add: `src/chc_rental/incremental.py`
- Add: `src/chc_rental/scheduler.py`
- Add: `src/chc_rental/sources/apify.py`
- Modify: `src/chc_rental/sources/base.py`, `sources/planner.py`, `sources/zillow.py`
- Modify: `src/chc_rental/cli.py`
- Add: `tests/test_incremental_scheduler.py`, `tests/test_apify_run_lifecycle.py`
- Extend: `tests/test_sources.py`

**CLI:**

```text
chc-rental alerts cycle --fixture PATH       # offline, no send
chc-rental alerts cycle --shadow             # live source, no send
chc-rental alerts status --json              # read-only operational status
```

**Tests:**

- Two identical searches owned by different people start one actor run.
- Different source-filter envelopes remain separate.
- A crash after actor start resumes the persisted run without a second charge.
- Five-run ceiling and global ceiling stop cleanly.
- Truncated/error runs never emit no-results conclusions.
- Sleep catch-up launches once.
- Existing daily fetch tests are unchanged and green.

**Exit gate:** three consecutive shadow cycles create correct source-run records,
stay within budget and generate no Telegram traffic.

**Rollback:** disable incremental globally; no legacy code or state was replaced.

## Phase 3 — Baseline, event classification and shadow outbox

**Objective:** turn observations into deterministic, per-user notification intent
without sending messages.

**Flow:**

```text
canonical records
  -> identity/version upsert
  -> baseline/new/change classification
  -> existing deterministic matcher
  -> allowlist recheck
  -> daily cap and quiet-hour calculation
  -> idempotent outbox insert (shadow status)
```

**Baseline rules:**

- The first usable result set for each source/query scope is stored as a silent
  baseline, including a bounded/truncated result window.
- A failed run cannot establish a baseline.
- Editing only local delivery settings does not reset a baseline.
- A material source query change creates a new fingerprint and therefore a new
  silent baseline.
- An operator-requested baseline reset requires confirmation and an audit record.

**Matching and caps:**

- Reuse `matching.py`; source push-down is optimization, not eligibility.
- Keep one notification per person/listing even when multiple searches match;
  retain all matching search IDs as explanation, with one primary search for cap
  attribution.
- Preserve each search's daily cap. Immediate mode changes timing, not limits.
- `notify_on_no_results` applies only to daily mode and is never evaluated per
  incremental cycle.

**Outbox behavior:**

- Insert intent and cap reservation in one SQLite transaction.
- `not_before` moves messages out of quiet hours in the recipient's local zone.
- Deactivating a person or search cancels its unsent rows at claim time.
- Shadow rows render the exact Telegram message but cannot be claimed by the live
  sender.

**Files:**

- Add: `src/chc_rental/events.py`, `src/chc_rental/outbox.py`
- Modify: `src/chc_rental/dedup.py`, `src/chc_rental/pipeline.py`
- Modify: `src/chc_rental/store.py`, `src/chc_rental/notification_schedule.py`
- Add: `tests/test_listing_events.py`, `tests/test_outbox.py`
- Extend: `tests/test_pipeline.py`, `tests/test_notification_schedule.py`

**Tests:**

- First usable run creates no outbox notification.
- A new item in the next run creates exactly one shadow row.
- Replaying the same payload creates nothing.
- A price change is recorded but not enqueued in MVP.
- Cross-source duplicate prefers a direct Zillow link without a second alert.
- Query change creates a silent baseline for only the new scope.
- Quiet hours handle midnight crossing and daylight-saving transitions.
- Concurrent cycles cannot overspend a daily search cap.

**Exit gate:** fixture replay demonstrates baseline -> one new alert -> no duplicate;
shadow results match the existing filter logic for Peter's searches.

**Rollback:** clear/move only the shadow ledger; daily seen ledgers remain untouched.

## Phase 4 — Dashboard control plane and observability

**Objective:** expose real incremental settings, health and queue state without
breaking the existing allowlist or preference menus.

This phase is required before live cutover. It is not cosmetic.

**Existing screens preserved:**

- Telegram allowlist menu;
- per-person saved preference menu and its immediate refresh after add/edit/
  toggle/delete;
- delivery settings;
- current daily/source status.

**Additions:**

1. Per-person Alert Settings:
   - delivery mode: Daily digest or Immediate alerts;
   - quiet hours in the profile timezone;
   - read-only count of pending/failed alerts.
2. Incremental Sources:
   - global incremental kill switch;
   - Zillow enabled state, actor, interval, daily ceiling, result cap and maximum
     charge per run;
   - explicit confirmation before enabling the flagged source;
   - RentCast state shown independently.
3. Health and coverage:
   - last attempt, last success, age/staleness and next due time per query scope;
   - running actor ID/status without token-bearing URLs;
   - baseline state, result count, truncation and errors;
   - runs used/remaining and known/unknown cost;
   - outbox pending, sent, failed and uncertain counts.
4. Operator actions:
   - pause/resume incremental globally;
   - run one shadow cycle;
   - retry a definitely failed outbox item;
   - baseline reset with confirmation;
   - no automatic retry button for ambiguous Telegram sends.

Every control writes through `TuiController` -> `Store`, immediately rereads the
persisted value and refreshes the visible screen. No widget may exist without a
tested runtime consumer.

**Files:**

- Add: `src/chc_rental/tui/alerts.py`
- Modify: `src/chc_rental/tui/app.py`, `tui/controller.py`, `tui/status.py`,
  `tui/forms.py`
- Extend: `tests/test_tui.py`
- Add: `tests/test_tui_alerts.py`

**Tests:**

- Existing add/edit/toggle/delete preference tests remain green.
- The saved-preference table and parent search count refresh after every mutation.
- Controls persist and affect scheduler/source/outbox behavior in integration tests.
- Token absence is shown as readiness state, never as token text.
- Source enable requires confirmation and cannot bypass global kill switch.
- Tables remain usable at 80x24; long actor/error text does not hide controls.

**Exit gate:** headless tests prove each displayed control changes shared runtime
state; dashboard and CLI status agree on the same fixture ledger.

**Rollback:** incremental screen can be hidden by the global feature flag; the
existing allowlist and preferences screens use the same preserved config data.

## Phase 5 — Peter-only live Telegram delivery

**Objective:** deliver new Zillow observations to Peter only while every other
person stays on the existing daily path.

**Delivery state machine:**

```text
shadow -> pending -> sending -> sent
                    |    |
                    |    +-> uncertain (process died in the remote-accept window)
                    +------> failed -> retry_wait -> pending
```

Telegram does not offer a general idempotency key for `sendMessage`, so absolute
exactly-once delivery cannot be promised across a crash after Telegram accepts a
message but before the local receipt commits. A stale `sending` row therefore
becomes `uncertain` and is not automatically resent. This makes the ambiguity
visible instead of silently duplicating or discarding it.

**Safety gates:**

- Add an operator-owned canary allowlist setting; initially Peter's Telegram ID
  is the only entry.
- Require all four gates: global live push, global incremental alerts, Zillow
  source enabled and recipient delivery mode `immediate`.
- Recheck active person and search immediately before claiming and again before
  transport.
- Mark `sent` and record the receipt only after the Telegram call returns success.
- Retry known failures with bounded exponential backoff; auth/chat-blocked errors
  become terminal and alert the owner once per failure streak.
- Do not import existing daily seen rows into the new outbox. The baseline absorbs
  the visible market and prevents a flood; cross-check old seen keys as an extra
  suppression layer during migration.

**Files:**

- Modify: `src/chc_rental/outbox.py`, `pipeline.py`, `notify/telegram.py`, `cli.py`
- Add: `src/chc_rental/delivery_worker.py`
- Add: `tests/test_delivery_worker.py`
- Extend: `tests/test_telegram_sender.py`, `tests/test_cli.py`

**Canary script:**

1. Leave live delivery off and establish Peter's silent baseline.
2. Replay the baseline: zero pending messages.
3. Inject one fixture listing after the baseline: one shadow message.
4. Confirm the rendered recipient, search, price and link.
5. Promote only that row or run one live canary cycle with explicit confirmation.
6. Confirm exactly one Telegram receipt.
7. Replay the same event: zero additional sends.
8. Perform one live Zillow cycle; only a genuinely post-baseline match may send.

**Exit gate:** Peter receives one correct controlled message, duplicate replay
sends nothing, and 48 hours of canary operation produce no unexplained duplicate,
budget breach or false no-results notification.

**Rollback:** remove Peter from the canary list or disable incremental globally.
His `delivery_mode` may return to `daily`; the original daily command remains
available.

## Phase 6 — Unattended scheduling and operational hardening

**Objective:** make the canary survive restarts, sleep, source failures and
budget exhaustion while the dashboard reports the truth.

**Runtime:**

- Add a separate disabled-by-default `com.chcrental.alerts` LaunchAgent rather
  than replacing `com.chcrental.daily`.
- Continue using the proven tmux/TCC launch pattern on this machine.
- Scheduler tick may be frequent and free; it starts an actor only when a query
  scope is due and budget is available.
- One whole-cycle lock plus per-query leases prevent manual/scheduled overlap.
- On wake/restart: resume running actors, recover expired leases, mark stale
  sends uncertain, then run at most one due collection per scope.

**Cost controls:**

- hard per-run charge cap;
- hard per-source and global daily start caps;
- configurable monthly soft budget based on recorded/estimated charges;
- circuit breaker for consecutive auth, malformed-payload or actor failures;
- `cost_unknown` blocks automatic cadence increases;
- dashboard projection:
  `active query scopes x runs/day x observed p95 cost/run x 30`.

**Health:**

- source freshness is measured from last successful usable run;
- no-results is not a health signal;
- alert owner once at failure-streak start, once on meaningful escalation and
  once on recovery;
- prune raw payloads separately from identity/outbox history;
- daily SQLite backup with retention and verified restore drill.

**Files:**

- Add: `scripts/com.chcrental.alerts.plist`
- Modify: `chc.sh`, `src/chc_rental/scheduler.py`, `store.py`, `tui/status.py`
- Add: `docs/INCREMENTAL_OPERATIONS.md`
- Add: `tests/test_recovery.py`, `tests/test_cost_controls.py`
- Extend: `tests/test_cli.py`, `tests/test_store.py`

**Tests:**

- manual and scheduled cycles cannot overlap;
- restart resumes actor without a duplicate start;
- budget exhaustion stops collection but not already queued delivery;
- auth failure opens the source breaker and recovery closes it;
- unchanged failure streak sends one owner alert;
- SQLite backup restores and passes integrity check;
- disabling either job leaves the other workflow unaffected.

**Exit gate:** two unattended days with correct source cadence, recovery after one
forced failure and no duplicate notifications.

**Rollback:** unload only the alerts LaunchAgent and disable incremental; keep or
restart the daily LaunchAgent.

## Phase 7 — Cutover and source expansion gate

**Objective:** expand only after the Zillow canary demonstrates acceptable
freshness, cost and reliability.

### Zillow rollout gate

Before adding another recipient:

- baseline flood prevention proven;
- p95 actor duration and cost/run recorded;
- projected monthly cost approved;
- observed query coverage/truncation visible;
- duplicate rate is zero in deterministic replay;
- no unexplained Telegram uncertain rows;
- dashboard and CLI agree on source/outbox state;
- kill switches tested live.

Move one recipient at a time from `daily` to `immediate`. Keep RentCast available
until Zillow has at least seven consecutive healthy operating days. Retirement
of the daily path is a later, explicit cleanup—not part of this plan.

### Future source onboarding template

Facebook Marketplace, Craigslist or any other source requires all of:

1. fresh terms/access review and explicit owner decision in `docs/SOURCES.md`;
2. no credential/session-cookie collection unless separately approved and safe;
3. named, pinned actor with cost model and maintenance signal;
4. raw payload fixtures and canonical mapping tests;
5. stable identity/provenance behavior;
6. query coverage and truncation semantics;
7. independent cadence, budget, health and kill switch;
8. shadow baseline and Peter-only canary;
9. failure isolation proving it cannot suppress another source's positive alerts.

Apify is the execution platform, not a blanket authorization or a universal
multi-site schema. No source is enabled simply because an actor exists.

## Phase-wide verification commands

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src
.venv/bin/chc-rental --root . alerts status --json
```

Network calls are excluded from ordinary tests. Each phase uses fixtures and
fake transports first. Live Apify or Telegram calls occur only at the named
shadow/canary gates and remain bounded by the existing local controls.

## Definition of done

The buildout is complete when:

- the old daily workflow is still runnable;
- Zillow incremental collection has a silent baseline per query scope;
- one new post-baseline match creates one durable per-user outbox entry;
- replay, cross-source duplication and overlapping searches do not duplicate it;
- quiet hours and daily caps are enforced transactionally;
- a confirmed Telegram send creates a receipt and is not sent again;
- failures, truncation, staleness, budgets and uncertain sends are visible;
- every dashboard control changes the state used by the scheduler or worker;
- Peter's 48-hour canary and two unattended-day operational gate pass;
- no Facebook/Craigslist code, login material or hidden source activation has
  entered the initial implementation.
