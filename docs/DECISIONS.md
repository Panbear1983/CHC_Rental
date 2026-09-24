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
- **Sole source (updated 2026-08-13): Zillow rental collection through the
  pinned Apify actor.** RentCast was removed — its address-only records had no
  listing link and gave recipients nothing actionable. Zillow provides real
  `zillow.com/homedetails/...` listing links.
- Zillow is owner-approved and terms-flagged; it stays gated behind
  `zillow_enabled` and an explicit `APIFY_TOKEN` (a token alone never activates
  it). It is not represented as Zillow-authorized.
- Daily API-call ceiling: 5/day for Zillow, 100/day global
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
  identical source/reason/raw failures are retained once per run day.
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
and the ten-minute delivery checker cannot repeat them within a day.

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

## 12. Rollout evidence and future-source gate — DECIDED 2026-08-12

- First-canary and recipient-expansion readiness are computed from the same
  shared function used by CLI and dashboard. The check is read-only and cannot
  enable a source, recipient or scheduler.
- Live scheduler ticks persist compact success/degraded evidence. Expansion
  requires a confirmed receipt plus a healthy retained 48-hour window; it does
  not infer success merely because a process exists.
- Human-only facts use named, confirmed, audited rollout attestations. Evidence
  text must be short and non-secret, and any attestation can be revoked.
- No Facebook Marketplace or Craigslist code is authorized. Every future source
  must pass `docs/SOURCE_ONBOARDING.md` and receive its own terms decision,
  schema fixtures, identity tests, budgets, breaker, baseline and canary.

## 13. Unified per-recipient delivery bank — DECIDED 2026-08-14

- Scheduled, incremental, and manual Test Push deliveries share one isolated
  bank per Telegram ID, keyed by normalized state/city/address/unit identity.
- Only listings with a confirmed Telegram receipt enter the bank. Multipart
  Test Pushes bank each accepted part immediately; previews, cancellations,
  definite failures, and unaccepted parts do not.
- All delivery paths take the same per-recipient transport lock and re-check
  the bank immediately before sending. A stale Test Push preview aborts and
  must be reviewed again.
- Normal Test Push previews contain unseen matches only, filtering the whole
  compatible cache before applying each preference cap. No extra paid scrape
  is started to fill the batch.
- When every compatible match is already banked, Test Push may resend only
  through the distinct repeat warning and confirmation. The repeat is audited
  but cannot restart the original suppression window.
- The existing `seen_retention_days` setting remains authoritative (90 days by
  default). Test Push bank events suppress scheduled delivery but are excluded
  from the scheduled due-time calculation.

## 14. Per-recipient routine delivery diary — DECIDED 2026-08-14

- `Edit` opens a member-details page with Profile and Routine diary tabs. The
  diary is intentionally routine-only: Test Push and immediate alerts remain
  in their existing audit systems and do not clutter this view. A narrowly
  recovered historical Test Push may be added at the owner's request, but must
  be labeled `TEST PUSH · LEGACY` and excluded from scheduled suppression.
- Routine payloads are staged in a file-backed append-only journal before
  transport. A verified recipient receipt marks them accepted; interrupted or
  ambiguous sends become uncertain and are not automatically retried.
- The diary retains exact outbound text, listing snapshots, links and receipts
  for 365 days (`delivery_history_retention_days`). This is independent of the
  90-day duplicate-suppression window.
- Accepted no-results messages appear as notices. Definite failures remain an
  internal retryable state; accepted and uncertain entries are operator-visible.
- Existing scheduled seen rows are imported idempotently on a best-effort
  basis. Missing historical facts stay unknown and are visibly labeled legacy.
- A Telegram ID change starts the new ID with an empty visible diary. The old
  ID's immutable journal remains on disk until normal retention expiry.

## 15. The Zillow actor bills per RESULT — DECIDED 2026-08-29

Supersedes the request-count ceilings in decisions #4 and #8 as the *spend*
control. Those ceilings remain, but they were never a budget.

**The finding.** `maxcopell/zillow-scraper` is `PAY_PER_EVENT` on
`apify-default-dataset-item` — **$0.0023 per result** on the FREE plan
(cheaper on paid tiers: BRONZE $0.002, SILVER $0.0017). Confirmed against
billed runs: every 25-result run billed exactly $0.0575. So one "request" in
`store.reserve_request` cost anywhere from $0.012 to $0.138 depending on how
many rows came back. The request ledger counts runs; the bill counts rows.

**The rent filter was never applied.** `rental_search_url` sent the rent
envelope as `filterState.price`. On a `/rentals/` URL Zillow reads `price` as
the for-sale **home value** band and ignores it; the rent filter key is `mp`
(monthly payment). Measured over 2026-08-21..28, only **36%** of paid results
were inside the requested envelope, and only 31% were both in-envelope and
new. Probe on 2026-08-29, same city and moment, 10 results each:
`price` → 4/7 in-band (leaking $3,970 / $4,500 / $4,695); `mp` → 7/7, floor
exactly $5,000. `price` is not sent alongside `mp`: if Zillow ever honored it
as home value the daily pool would silently go empty.

**Measurements that set the ceilings** (2026-08-29, Brooklyn 3-5bd, $5k-$10k,
2+ baths):

| | |
|---|---|
| in-band share, before / after | 36% / 100% |
| in-band listings in a 60-slot window | 54, of which 45 had never been seen |
| daily in-band arrivals (`doz=1`) | ~26 |
| out-of-city share of every run | ~27% (structural, see below) |

**Decisions.**

- Spend is metered in RESULTS, by `chc_rental.cost`, against Apify's own
  reported cycle usage — not a local ledger. A local count cannot see anything
  else spending the same token, and something else was: a second project ran
  `zillow-detail-scraper` on this account daily until 2026-08-27, taking ~40%
  of the cap. The usage read is free.
- `zillow_budget_target_share: 0.85`. The remaining 15% is never spent.
  Reaching the real cap returns HTTP 403 `platform-feature-disabled` on every
  run and stops the whole product, as it did on 2026-08-20.
- `zillow_results_limit: 60`, throttled down by the gate when the cycle is
  tight and refused entirely below `zillow_min_results_floor: 10`.
- An unreadable usage endpoint falls back to the configured limit rather than
  blocking the scrape. The configured limit is itself bounded to fit a cycle.
- `zillow_days_on_zillow` (`doz`) bounds the window by recency instead of by
  our own slot count, so a large limit cannot re-buy listings the seen ledger
  already holds. It must be an **integer** in the URL: `{"value": 1}` works,
  `{"value": "1"}` makes the actor reject the whole URL. Set to `null` while
  backfilling; `1` in steady state (~$0.064/day vs ~$0.138/day).
- The daily adapter starts runs ASYNCHRONOUSLY and polls. Holding the run open
  on `run-sync-get-dataset-items` meant a dropped connection bought a second
  run while the first finished and billed anyway — 2026-08-23 and 2026-08-28
  each show two billed 25-result runs for one day's listings.

**Known, accepted cost: ~27% of every run is out-of-city.** The actor requires
a rectangular map bound and cities are not rectangles. Nominatim's rectangle
for Brooklyn also covers Lower Manhattan, part of Jersey City and western
Queens; `matching.py` rejects those locally, but they were paid for. Measured
2026-08-29: 13 of 49 results (7 Manhattan, 3 Jersey City NJ, 3 Queens). No
rectangle can isolate Brooklyn, so this is not fixable by tightening bounds.
The available fix is Zillow `regionSelection` region IDs, which would have to
be hand-configured per city — resolving them automatically would mean querying
Zillow directly, which SOURCES.md forbids. Not taken now; recorded so the cost
is visible and the option is not rediscovered from scratch.

`fetch.py` records `envelope_audit` per run and counts these two leaks
SEPARATELY: `outside_filters` (a filter regression — warns above 20%) and
`outside_city` (the structural cost above — recorded, never warned). Folded
together, the known 27% would sit permanently above any threshold and drown
the signal the warning exists to carry.
