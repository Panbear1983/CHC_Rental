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
- Do not scrape: Zillow, Realtor.com, Craigslist, Facebook Marketplace,
  Trulia, Zumper, PadMapper, Apartments.com consumer sites — no appropriate
  public consumer API and/or terms prohibit automated collection.
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

## 4. Primary licensed source + budget — PENDING

- Candidate primary source: RentCast paid/licensed API (recommended by the
  plan, subject to Peter accepting plan/price/terms). Alternative: defer
  individual listings until RESO/MLS vendor access exists.
- Daily API-call ceiling: ______
- Monthly spend ceiling: ______
- Behavior at ceiling: stop fetching and flag operator (plan default) — confirm.

## 5. Retention periods — PENDING

- Telegram IDs / profile audit history: ______
- Fetched listing cache: ______
- Notification/dedup ledger: ______
- Rejected/validation records: ______

## 6. Excess-demand behavior — PENDING

Proportional quotas vs strict equal round-robin.
Plan recommendation: strict equal round-robin initially.

## 7. Notification policy — PENDING

Plan default to confirm: Telegram delivery only after all validation; failed
delivery remains retryable with bounded retries (delivery ledger already
implements `max_attempts = 3`).

## 8. Circuit-breaker limits — PENDING

Concrete daily global limit for the budget ledger (code default currently
`DEFAULT_DAILY_BUDGET_LIMIT = 100` in `tui/controller.py`) — confirm or change.

## 9. Quarterly terms review + kill switch — PENDING

Approve a quarterly terms/source review cadence and a kill switch for each
source adapter; name the owner of that review.
