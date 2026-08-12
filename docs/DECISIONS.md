# CHC_Rental — Phase 0 Product Decisions

Decision record required by the foundation plan
(`.hermes/plans/2026-08-07_060000-chc-rental-foundation.md`, Phase 0).
Live source wiring (Phase 3) and Telegram wiring (Phase 5) stay blocked until
every PENDING item below is DECIDED.

## Standing design boundaries (non-negotiable, restated from the plan)

- One-way **outbound notification only**. No Telegram poller, webhook, or
  inbound message handler; no user self-enrollment path.
- Listing qualification is deterministic. LLMs may never determine whether a
  listing is eligible, invent a feature, scrape a page, or bypass a source
  restriction.
- No direct consumer-site scraping or bot-protection bypassing. Realtor.com,
  Craigslist, Facebook Marketplace, Trulia, Zumper, PadMapper and
  Apartments.com remain refused. Peter explicitly approved the separately
  flagged, managed Apify Zillow rental route on 2026-08-11; it stays disabled
  by default and does not represent Zillow authorization.
- All source calls obey published API/contract limits and a local hard daily
  circuit breaker.
- No external source credentials or Telegram tokens in tracked files.

## 1. Build order — DECIDED 2026-08-07

Build the TUI/profile foundation first and defer selection/activation of an
individual-listing source. Phase 1–2 may proceed offline; Phase 3 remains
blocked until a licensed source and its budget/terms are approved.

## 2. Allowlist authority — DECIDED 2026-08-07

Owner-controlled TUI only. Never self-enrollment through Telegram.

## 3. Python version pin — DECIDED 2026-08-10

`requires-python = ">=3.11,<3.12"` in `pyproject.toml` is authoritative (the
working venv runs 3.11). The plan's "Python 3.12+" stack note is superseded.

## 4. Listing sources + budget — DECIDED 2026-08-11

- Scope: **US rentals only** (Peter, 2026-08-11). Taiwan portals out of scope.
- Primary source: **RentCast** official API (`docs/SOURCES.md`), key already
  provisioned in `.env`.
- Additional source: owner-approved, terms-flagged Zillow rental collection
  through the pinned Apify actor. It is opt-in (`zillow_enabled: false` by
  default) and isolated so its failure cannot erase RentCast matches.
- Daily API-call ceiling: 50/day legacy/default for RentCast, 5/day for Zillow,
  100/day global
  (`config/settings.yaml`, enforced by `store.reserve_request`).
- Zillow per-query result limit: 25; per-actor-run charge ceiling: USD 0.25.
- Zillow search URLs must contain geographic map bounds. Resolve US city/state
  bounds through OpenStreetMap Nominatim, then day-cache the resulting source
  pool. Do not treat actor error markers or incomplete building-summary cards
  as listings.
- Monthly spend ceiling: whatever the currently provisioned RentCast plan
  includes — the daily ceilings above are set so the plan cap cannot be
  exceeded; revisit if the plan tier changes.
- Behavior at ceiling: stop fetching, deliver whatever the day cache already
  holds, and alert the operator via Telegram.
- Cache reuse requires an exact normalized city/state query scope. A changed
  allowlist scope forces a refresh; local-only price/bed/filter edits continue
  to reuse the existing city pool. Truncation and per-query errors survive
  cache reuse and suppress no-results notices.

## 5. Retention periods — DECIDED 2026-08-11 (defaults confirmed)

- Notification/dedup (seen) ledger: 90 days (`seen_retention_days`) — after
  this, an old listing may notify again; accepted.
- Fetched listing cache: 7 days. Rejected/validation records: 30 days;
  identical source/reason/raw failures are retained once per UTC day.
- Profile audit history: gitignored config + timestamped `backups/`, 30 days.
- All values live in `config/settings.yaml` and are revisable without
  migration.

## 6. Excess-demand behavior — DECIDED 2026-08-11

Per-search daily caps only, no cross-person round-robin, while the allowlist
holds two people. Revisit (plan recommends strict equal round-robin) before
the allowlist grows or budget pressure appears.

## 7. Notification policy — DECIDED 2026-08-11

Telegram delivery only after full validation. A failed listing push is not
marked seen, so it retries on the next run — bounded to one attempt per run
by design (the old ledger's `max_attempts = 3` is superseded by the daily
cadence). No-results notices stamp the seen ledger so the due-gate advances
and the hourly runner cannot repeat them within a day.

## 8. Circuit-breaker limits — DECIDED 2026-08-11

Global 100/day, legacy/default per-source 50/day, and explicit Zillow 5/day
confirmed, enforced in `config/settings.yaml` + `store.reserve_request`. The
Zillow result and per-run charge caps are additional hard controls.

## 9. Quarterly terms review + kill switch — PENDING (owner needed)

Kill switches exist: `live_push_enabled: false` stops all sends;
removing/emptying `RENTCAST_API_KEY` (or the per-source ceiling set to 0)
stops RentCast; `zillow_enabled: false`, an absent `APIFY_TOKEN`, or a Zillow
request budget of 0 stops Zillow. Still needed from Peter: commit to a
quarterly terms/source review cadence and own it.

## 10. Incremental alert architecture — DECIDED 2026-08-12

- Product mode: add a passive, near-real-time alert path for listings first
  observed after a silent source/query baseline. The current daily workflow
  remains available until an explicit cutover.
- Initial incremental source: the existing flagged Zillow/Apify route only.
  RentCast remains installed as the licensed daily fallback/control source.
- Facebook Marketplace and Craigslist remain refused. The Zillow exception is
  not a general approval for Apify actors or consumer-site collection.
- Existing profiles retain daily delivery by default. Immediate delivery must
  be selected explicitly and initially applies only to the Peter canary.
- Runtime state: YAML remains the owner-controlled configuration authority. A
  local SQLite operational ledger may hold source runs, observations, baselines,
  notification intent and delivery receipts; it is not a property database and
  has no property-search UI.
- Notification event for the first release: new listing only. Price changes,
  material changes and repost candidates may be recorded but are not pushed.
- First usable run for a source/query is silent. Failed runs cannot establish a
  baseline; truncated runs provide positive evidence only and never justify a
  no-results conclusion.
- Budget truth: the current five Zillow actor starts per day are shared by all
  active incremental query scopes. A shorter advertised freshness target is
  blocked until measured actor cost and a monthly ceiling are approved.
- Rollout: fixture -> shadow collection -> shadow outbox -> dashboard control ->
  one controlled Peter notification -> 48-hour canary -> one recipient at a
  time. See `.hermes/plans/2026-08-12_120000-chc-rental-incremental-apify-alerts.md`.

## 11. Incremental operations and activation — DECIDED 2026-08-12

- The existing daily LaunchAgent stays intact. Incremental work has its own
  `com.chcrental.alerts` job, which ships disabled and is not installed or
  loaded by the build.
- One shared whole-run lock prevents daily, manual shadow and incremental
  scheduler overlap. Persisted Apify run IDs are resumed rather than restarted.
- Per-run, source-daily and global-daily ceilings are hard stops. The monthly
  ceiling is a configurable soft stop for new paid starts; running actors and
  queued delivery still recover. Unknown charge data blocks automatic cadence
  increases.
- Source health uses a persisted circuit breaker. Auth failures open it
  immediately; other failures use the configured threshold. The owner is
  notified once at failure-streak start, on meaningful escalation/open, and on
  recovery.
- Incremental operational history defaults to 180 days. Problem outbox rows and
  active query scopes are retained. The ledger is backed up daily, verified,
  and restore-tested only into a disposable database.
- Activation remains a separate operational decision after a controlled Peter
  canary. This implementation does not load the job, enable the gates, or make
  an external call by itself.
