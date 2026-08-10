# CHC_Rental

Local, offline, preference-driven rental-listing notifier foundation.
SQLite is the source of truth; a Textual TUI is the owner's control plane for
allowlisted Telegram users and their preference profiles; deterministic
matching, dedup, and a fair daily scheduler decide what would be notified.
Everything currently in this repo is offline: no listing source is queried
and no messages are sent.

Build status by phase (plan: `.hermes/plans/2026-08-07_060000-chc-rental-foundation.md`,
decisions: `docs/DECISIONS.md`):

- Phase 1 — offline allowlist/profile foundation: **built**
- Phase 2 — Textual owner dashboard: **built**
- Phase 3 — licensed listing-source adapter: **not built** (blocked on
  `docs/DECISIONS.md` pending items)
- Phase 4 — matching, dedup, fair scheduler, budgets: **built**
- Phase 5 — outbound Telegram notifier: **offline transport seam built**;
  no credential loading, network transport, or live send is enabled.
- Phase 6 — market context: not built

## Architecture note: one-way notification, no inbound poller

This codebase does not run a Telegram poller, webhook, or any inbound
message handler, and Phase 1 contains no Telegram network code at all. The
intended end-state architecture (later phases) is **one-way outbound
notification only**: the owner manages allowlisted users and their
preference profiles locally, and a future notifier would push qualifying
listings out to already-allowlisted users. There is no user self-enrollment
path and no code here that reads inbound Telegram messages or user
interaction of any kind.

## What's included

- `src/chc_rental/models.py` — Pydantic models: `AllowlistedUser`,
  `PreferenceProfile` (create/update variants), `Listing`, `AuditEvent`,
  `DeliveryRecord`, `BudgetState`, `PropertyType`.
- `src/chc_rental/db.py` — SQLite schema (`allowlisted_users`,
  `preference_profiles`, `audit_events`, `delivery_ledger`,
  `daily_budget_ledger`), connection management, and additive column
  migrations for pre-existing databases.
- `src/chc_rental/repositories.py` — `AllowlistRepository`,
  `ProfileRepository`, `AuditRepository`. All profile access is gated on
  allowlist membership; cross-user profile access is rejected; denied
  attempts are recorded as audit events.
- `src/chc_rental/matching.py` — deterministic listing-to-profile
  eligibility from typed fields only.
- `src/chc_rental/dedup.py` — listing dedup keys for the delivery ledger.
- `src/chc_rental/scheduler.py` — fair round-robin daily allocation across
  active profiles under per-profile caps and the global budget.
- `src/chc_rental/budgets.py` — daily budget ledger + circuit breaker.
- `src/chc_rental/delivery.py` — persistent delivery ledger with bounded
  retries.
- `src/chc_rental/notification_schedule.py` — per-profile local-time due
  calculation (no transport, no daemon).
- `src/chc_rental/dry_run.py` — fixture-only, read-only daily pipeline preview:
  validate → match → dedupe → allocate → schedule → delivery-ledger eligibility.
- `src/chc_rental/db_recovery.py` — explicit local SQLite health check, backup,
  reviewed legacy migration, and refusal/audit report for unknown or corrupt DBs.
- `src/chc_rental/outbound.py` — typed, recipient-free notification renderer and
  disabled-by-default dedicated-Buddy transport seam; no HTTP/credentials/live send.
- `src/chc_rental/status.py` — read-only operator status snapshot.
- `src/chc_rental/errors.py` — domain exceptions.
- `src/chc_rental/tui/` — Textual owner dashboard (`app.py`), service layer
  (`controller.py`), form coercion (`forms.py`), status screen (`status.py`).

## Enforced profile rules

- `city` required; `district` optional.
- `price_min <= price_max`.
- `property_types` normalized (trimmed/lowercased) and restricted to a known
  set (`apartment`, `house`, `condo`, `townhouse`, `studio`, `room`); at
  least one is required.
- `bed_min <= bed_max`, `bath_min <= bath_max`.
- `sqft_min` / `sqft_max` optional bounds (blank = no bound;
  `sqft_min <= sqft_max` when both set). Listings without square footage are
  not rejected by sqft bounds.
- `required_features` / `excluded_features` normalized; a feature cannot be
  in both lists at once.
- `daily_cap` must be a positive integer.
- `delivery_time` strict 24-hour `HH:MM`; `timezone` a valid IANA zone.
- `notify_on_no_results` defaults to `False`; when enabled, a due profile may
  receive an explicit no-match message in a later authorized delivery run.
- `active` toggle, defaults to `True`.
- One allowlisted Telegram user ID may own multiple profiles, distinguished
  by unique `profile_name` per user.
- A user not on the allowlist cannot create, list, read, update, or delete
  any profile.
- A user may not read, update, or delete another user's profile.
- Profile creation, updates, deletions, and denied-access attempts are all
  recorded in `audit_events`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run the owner dashboard (TUI)

```bash
.venv/bin/chc-rental-tui                 # default DB: data/chc_rental.sqlite3
.venv/bin/chc-rental-tui --db-path path/to/other.sqlite3
```

(Or `source .venv/bin/activate && chc-rental-tui`.) The dashboard manages the
allowlist, per-user preference profiles, and a read-only status screen
(budget/circuit-breaker state, delivery counts, per-profile caps).

## Offline operational scripts

### Fixture-only daily preview

This is read-only: it cannot fetch listings, consume a budget, mutate the
ledger, or send a message. Supply only a local JSON fixture whose records fit
the `Listing` model.

```bash
.venv/bin/python -m chc_rental.dry_run \
  --db data/chc_rental.sqlite3 \
  --fixture fixtures/listings.json \
  --now 2026-08-10T12:00:00Z \
  --global-daily-budget 20
```

### Database health and bounded recovery

Check is read-only. `repair` first creates a timestamped SQLite backup and
only applies an exactly-recognized legacy schema migration. Unknown schemas or
integrity failures refuse repair and produce an owner-action-needed JSON report.

```bash
.venv/bin/python -m chc_rental.db_recovery check data/chc_rental.sqlite3
.venv/bin/python -m chc_rental.db_recovery repair path/to/known-legacy.sqlite3
```

## Run tests

```bash
pytest -q
```

## Environment

Copy `.env.example` to `.env` for later phases. Current code reads only
`CHC_RENTAL_DB_PATH` (optional); no credentials are read or sent anywhere.

```bash
cp .env.example .env
```

## Not built yet

- No listing-source scraper or API client (Phase 3 — blocked on
  `docs/DECISIONS.md`).
- No Telegram bot, poller, webhook, or any network transport (Phase 5
  transport pending).
- No `cli.py` / daily-run entry point.
- No network calls of any kind.
