# CHC_Rental Foundation Implementation Plan

> **For Hermes:** Execute only after Phase 0 decisions are recorded. Use separate, file-scoped agents: Claude Sonnet for source/compliance review and Codex for implementation/tests. One writer per file set; no concurrent edits in the main worktree.

**Goal:** Build a local, preference-driven rental-listing system with a terminal TUI, per-allowlisted Telegram-user profiles, compliant licensed listing-source adapters, deterministic matching/validation, fair daily allocation, and notification-ready results.

**Architecture:** A local Python service uses SQLite as the source of truth. The Textual TUI is the operator control plane for allowlisted Telegram users and their multiple preference profiles. A pluggable, licensed source adapter retrieves listings; a deterministic normalizer, deduper, feature matcher, and validation gate decide eligibility. A round-robin scheduler fairly allocates the bounded daily fetch/notification budget across active profiles.

**Tech stack:** Python 3.12+, Textual, SQLite, Pydantic, HTTPX, APScheduler (or a narrow internal scheduler), pytest. Initial source adapter: **pending primary-source decision**; no website HTML scraping or bot-protection bypassing.

---

## Research outcome: lawful source boundary

| Source | Intended role | Status |
|---|---|---|
| RentCast API | Licensed individual rental-listing source, subject to plan/terms review | Candidate primary source |
| RESO Web API / MLS Grid | Future licensed MLS route, subject to broker/vendor agreement | Future option |
| HUD FMR API | Price-context / validation benchmark only | Optional supplement |
| Redfin Data Center | Aggregate market-context / validation only | Optional supplement |
| Zillow, Realtor.com, Craigslist, Facebook Marketplace, Trulia, Zumper, PadMapper, Apartments.com consumer sites | Do not scrape: no appropriate public consumer API and/or terms prohibit automated collection | Explicitly excluded |

Research references:
- https://developers.rentcast.io/
- https://developers.rentcast.io/reference/rental-listings-long-term
- https://www.reso.org/reso-web-api/
- https://www.huduser.gov/portal/dataset/fmr-api.html
- https://www.redfin.com/news/data-center/

## Acceptance rules

- Every Telegram user must be allowlisted before their profiles can be viewed, altered, scheduled, or notified.
- A Telegram user can own multiple named profiles; each profile has its own city, district, price range, property type, bed/bath range, feature rules, active state, and daily listing cap.
- The scheduler must make allocation deterministic and fair across all active profiles: round-robin by profile, then per-profile cap, then source-wide budget.
- Listing qualification is deterministic. LLMs may never determine whether a listing is eligible, invent a feature, scrape a page, or bypass a source restriction.
- All source calls obey published API/contract limits and a local hard daily circuit breaker.
- Unknown/invalid listings are retained as validation outcomes for operator review, not silently notified or silently treated as valid.
- No external source credentials or Telegram tokens are placed in tracked files.

## Phase 0 — Required product decisions (no implementation)

**Objective:** Freeze the non-code policy that determines the safe product boundary.

1. Choose the initial primary source:
   - **Recommended:** RentCast paid/licensed API, after Peter accepts the plan/price/terms.
   - Alternative: defer individual listings until RESO/MLS vendor access exists.
2. Set the initial source quota/cost ceiling:
   - daily API-call ceiling;
   - monthly spend ceiling;
   - behavior at ceiling: stop fetching and flag operator.
3. Confirm allowlist authority: owner-controlled TUI only, not self-enrollment through Telegram.
4. Choose retention periods for:
   - Telegram IDs/profile audit history;
   - fetched listing cache;
   - notification/dedup ledger;
   - rejected/validation records.
5. Choose excess-demand behavior: proportional quotas or strict equal round-robin. **Recommended:** strict equal round-robin initially.
6. Choose notification policy: Telegram delivery only after all validation; failed delivery remains retryable with bounded retries.
7. Approve quarterly terms/source review and a kill switch for each adapter.

**Recorded decision (2026-08-07):** Build the TUI/profile foundation first and defer selection/activation of an individual-listing source. Phase 1–2 may proceed offline; Phase 3 remains blocked until a licensed source and its budget/terms are approved.

**Exit criterion:** These decisions are recorded in `docs/DECISIONS.md` before any live source/Telegram wiring.

## Phase 1 — Project skeleton and policy model

**Objective:** Build an offline, testable local foundation—no real scraping or Telegram sends.

**Files:**
- Create: `pyproject.toml`
- Create: `src/chc_rental/config.py`
- Create: `src/chc_rental/models.py`
- Create: `src/chc_rental/db.py`
- Create: `src/chc_rental/repositories.py`
- Create: `src/chc_rental/allowlist.py`
- Create: `tests/test_allowlist.py`
- Create: `tests/test_profile_repository.py`
- Create: `docs/DECISIONS.md`

**TDD slices:**
1. Write failing test: unknown Telegram ID cannot access profile storage.
2. Add minimum `allowlisted_users` SQLite table and owner-only repository operations.
3. Write failing test: one allowlisted user can create two named profiles without cross-user visibility.
4. Add `preference_profiles` schema and typed Pydantic model.
5. Enforce: city, optional district, `price_min <= price_max`, known property types, bed/bath ranges, normalized feature include/exclude lists, positive daily cap.
6. Run: `pytest tests/test_allowlist.py tests/test_profile_repository.py -q`.

## Phase 2 — Textual TUI preference dashboard

**Objective:** Let the owner manage allowlisted Telegram users and profile preferences locally.

**Files:**
- Create: `src/chc_rental/tui/app.py`
- Create: `src/chc_rental/tui/screens/users.py`
- Create: `src/chc_rental/tui/screens/profiles.py`
- Create: `src/chc_rental/tui/widgets/preference_form.py`
- Create: `tests/test_tui_profile_form.py`
- Modify: `README.md`

**TDD slices:**
1. Test form rejects inverted price and bed/bath ranges.
2. Implement profile form fields: city, district, price min/max, property types, bed/bath min/max, required/excluded features, profile daily cap, active toggle.
3. Test one user can maintain multiple named profiles.
4. Add user selector and profile list/detail/edit flow.
5. Add confirmation for deleting a profile; preserve an audit event, not a raw record delete.
6. Run focused TUI tests without a network source.

## Phase 3 — Source contract and one licensed adapter

**Objective:** Add a source-agnostic interface and exactly one approved source adapter; manual fetch only.

**Files:**
- Create: `src/chc_rental/sources/base.py`
- Create: `src/chc_rental/sources/rentcast.py` *(only if approved in Phase 0)*
- Create: `src/chc_rental/normalization.py`
- Create: `src/chc_rental/validation.py`
- Create: `src/chc_rental/listings.py`
- Create: `tests/test_normalization.py`
- Create: `tests/test_source_contract.py`
- Create: `tests/test_validation.py`

**TDD slices:**
1. Define a `ListingSource` interface whose implementation receives a typed profile query and returns typed raw records.
2. Use fixtures, not live API calls, to prove source-field mapping into canonical `Listing` records.
3. Validate HTTPS link, source listing ID, positive price, sensible location, source freshness, and required address/location fields.
4. Deterministic feature extraction may inspect only returned API fields and permitted listing description data; it must return a trace of matched source text/structured fields.
5. Reject or mark uncertain unsupported claims; never use an LLM to decide suitability.
6. Implement TUI manual-run preview that reports retrieval/validation counts but does not notify.

## Phase 4 — Matching, deduplication, and fair daily scheduler

**Objective:** Produce a bounded, reproducible daily candidate set across all profiles.

**Files:**
- Create: `src/chc_rental/matching.py`
- Create: `src/chc_rental/dedup.py`
- Create: `src/chc_rental/scheduler.py`
- Create: `src/chc_rental/budgets.py`
- Create: `tests/test_matching.py`
- Create: `tests/test_scheduler_fairness.py`
- Create: `tests/test_dedup.py`

**TDD slices:**
1. Test exact profile match behavior for city/district/price/type/beds/baths and include/exclude features.
2. Test a stable identity hash from source ID when present, otherwise normalized address + unit + source; do not merge uncertain listings silently.
3. Test equal round-robin: each active profile receives one query turn before any profile receives another.
4. Test each profile’s daily cap and the global source call/listing budget cannot be exceeded.
5. Persist per-user/per-listing delivery state so a listing that becomes valid later is not lost or repeatedly sent.
6. Surface quota depletion and validation failures in TUI status.

## Phase 5 — Telegram notification adapter and operational hardening

**Objective:** Safely send qualifying listings only to their allowlisted owner, with no second poller or unwanted notification spam.

**Files:**
- Create: `src/chc_rental/notifications/base.py`
- Create: `src/chc_rental/notifications/telegram.py`
- Create: `src/chc_rental/cli.py`
- Create: `tests/test_notification_dispatch.py`
- Create: `tests/test_daily_run.py`
- Modify: `README.md`
- Create: `docs/OPERATIONS.md`

**TDD slices:**
1. Inject a fake Telegram transport and test exact recipient routing from allowlisted profile ownership.
2. Test delivery is marked sent only after transport success; failures are bounded-retry and visible to the operator.
3. Add one-run CLI mode and lock it to prevent overlapping scheduled runs.
4. Add source adapter circuit breaker, timeout/backoff, per-source kill switch, and structured audit logs without tokens/IDs in logs.
5. Before a real scheduled deployment, run an owner-approved single-profile/single-listing smoke test only.

## Phase 6 — Optional market context and second source

**Objective:** Add price-context enrichment only after the first source is operating safely.

**Files:**
- Create: `src/chc_rental/sources/hud.py`
- Create: `src/chc_rental/sources/redfin_context.py`
- Create: `tests/test_market_context.py`

HUD/Redfin information must be labeled as aggregate market context—not individual listing availability or suitability—and must never replace the primary licensed listing source.

## Verification checklist before live use

```bash
python3 -m pytest -q
python3 -m ruff check src tests
python3 -m mypy src
python3 -m py_compile $(find src -name '*.py')
```

- Test offline fixtures for each source adapter; do not use production API calls for ordinary tests.
- Verify no duplicate Telegram poller/scheduler starts in staging.
- Verify the configured source key/token is loaded only from `.env` and never logged.
- Verify fair scheduler behavior from persisted records, including profile caps and global circuit breaker.
- Perform one owner-approved end-to-end notification smoke test after explicit source/budget approval.

## Current missing decisions

The requested preferences are covered. The build still needs these decisions before Phase 1/3 live integrations: primary licensed listing source and budget, user/record retention, allowlist owner, demand-over-quota policy, notification retry policy, and circuit-breaker limits.
