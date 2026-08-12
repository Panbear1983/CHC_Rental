# Incremental source onboarding gate

Apify is an execution platform, not a blanket source approval. Every new
listing source gets an independent decision, adapter, budget, breaker, baseline
and canary. A working actor alone is not sufficient.

## Required source record

Before code is added, create a section in `docs/SOURCES.md` that records:

1. `ALLOWED`, `ALLOWED-WITH-LIMITS`, `OWNER-APPROVED-FLAGGED`, or `REFUSED`;
2. the terms/access review date, links and explicit owner decision;
3. the exact access path, named actor/API version and maintainer signal;
4. whether login material, session cookies, CAPTCHA handling or proxying is
   required—those require a separate security decision and are not inherited;
5. hard per-run and daily ceilings plus an approved monthly cost envelope;
6. the source-specific kill switch and credential name;
7. known coverage, sorting, pagination and truncation behavior.

## Required implementation evidence

Each accepted source must supply all of the following before its gate can be
enabled:

- captured raw fixtures for normal, empty, malformed, auth-failed, rate-limited
  and truncated responses;
- a source-owned mapper into the canonical listing model, with provenance and
  stable identity tests;
- deterministic replay proving the same observation cannot create a second
  recipient outbox item;
- an independent due schedule, hard request/charge caps, monthly projection,
  persisted breaker and owner health alerts;
- explicit query coverage and result-order semantics; an empty or truncated
  dataset may never be presented as proof that the market has no matches;
- failure-isolation tests proving one source cannot suppress another source's
  positive alerts or already-queued Telegram delivery;
- a silent baseline, fixture/shadow run, Peter-only controlled receipt and a
  healthy observation window before any other recipient is added.

The common layer may share canonical matching, identity, observation and outbox
machinery. Actor input, dataset normalization, error classification, cadence,
budgets, terms status and kill switches remain source-specific. There is no
universal multi-site actor payload or raw-data schema.

## Current expansion status

Facebook Marketplace and Craigslist remain `REFUSED` under the current source
record. No adapter, login/session material or hidden feature gate for either is
part of the Zillow build. A future owner decision must update `docs/SOURCES.md`
before implementation begins, then pass this entire gate independently.

Use `./chc.sh alerts readiness --json` for the Zillow rollout evidence. Local
operator attestations are intentionally explicit and audited; never put tokens,
cookies, chat IDs or other secrets in an attestation's evidence text.
