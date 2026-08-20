# Listing sources — vetting record (Phase 0)

Required by the buildout plan: no adapter may be activated for a source without
an explicit status here. Scope decision (Peter, 2026-08-11): **US rentals only.**

## RentCast — REMOVED 2026-08-13

Status: **REMOVED.** RentCast was the primary source through 2026-08-12 but was
removed on 2026-08-13 (Peter): its records carried **no consumer listing link**
— only a Google-Maps-of-the-address fallback — which delivered no value to
recipients. The adapter (`sources/rentcast.py`) and its wiring were deleted;
Zillow via Apify is now the sole source. The unused `RENTCAST_API_KEY` may be
removed from `.env` at will.

## Zillow rentals through Apify — OWNER-APPROVED-FLAGGED

| | |
|---|---|
| Status | **OWNER-APPROVED-FLAGGED**; disabled by default; not represented as Zillow-authorized |
| Approved | 2026-08-11 by Peter for this private rental-link workflow |
| Access | Managed Apify actor over its HTTPS API; local secret is `APIFY_TOKEN` |
| Actor | `maxcopell/zillow-scraper`, pinned through `zillow_actor` settings |
| Actor/API docs | https://apify.com/maxcopell/zillow-scraper and https://apify.com/maxcopell/zillow-scraper/api |
| Bounds lookup | OpenStreetMap Nominatim search API; city/state to map rectangle only; no listing data or credentials |
| Zillow terms checked | 2026-08-11; https://www.zillow.com/corporate/terms-of-use/ prohibits automated queries and encouraging third parties to perform them |
| Local ceilings | Disabled by default; one actor run per watched city per day (budget 5/day); 25 results/run; USD 0.25 maximum charge/run; 100 requests/day global |
| Subscription ceiling | The Apify plan's own monthly cap sits ABOVE all of these and is not enforced locally. Reaching it returns HTTP 403 `platform-feature-disabled` on every run and stops the product outright. Check it with `./dashboard.sh usage`. |
| Kill switch | `zillow_enabled: false`, missing token, zero results, or zero request budget |

Implementation boundaries:

- CHC_Rental never directly requests Zillow pages and contains no CAPTCHA,
  proxy, login or anti-bot bypass code. The managed actor receives a rental
  search URL and returns a bounded dataset.
- The actor requires a map-backed search URL. CHC resolves US city/state bounds
  through Nominatim with an identifying user agent and caches the resulting
  Zillow pool for the configured scrape timezone's calendar day. Ten-minute
  checks reuse it and do not repeat the lookup.
- Only explicit rental results are admitted. Explicit sale/sold results are
  discarded. Actor control records and building-summary cards without exact
  unit-level bath/price data are skipped rather than fabricated; other
  malformed records go through the normal rejected-record path.
- A partial Zillow pool may still produce positive matches, but no-results
  notices are suppressed unless the enabled source produced complete coverage.
- Search-card results have no trusted amenity list or neighborhood. Features
  stay empty; a missing district can match only when the requested district is
  exactly the listing city.
- This approval is a product-risk decision, not legal advice or a finding that
  the managed collection complies with Zillow's terms.

## Carried-forward exclusions — REFUSED

Direct Zillow collection, Realtor.com, Craigslist, Facebook Marketplace,
Trulia, Zumper, PadMapper and Apartments.com consumer sites remain refused:
no appropriate public consumer API and/or terms prohibit automated collection.
The tightly bounded managed Zillow exception above does not generalize to these
sites or authorize a direct scraper.

## Incremental-source policy — DECIDED 2026-08-12

The first incremental-alert build uses only the pinned Zillow actor above. Its
results are treated as bounded observations, not as a complete representation
of the market:

- the first usable result window establishes a silent baseline;
- hitting `resultsLimit` marks the window truncated;
- a truncated or failed run never proves that no matching listing exists;
- newly observed post-baseline records may produce positive alerts after local
  deterministic matching;
- actor output changes are admitted only after fixture/contract tests pass;
- credentials, login sessions, CAPTCHA solving and local bot-bypass code remain
  outside the project.

RentCast stays available during the incremental canary but is not the primary
link/freshness source. Facebook Marketplace and Craigslist require new, explicit
source decisions and do not enter the initial implementation.

The complete evidence and isolation checklist for any future source is
[SOURCE_ONBOARDING.md](SOURCE_ONBOARDING.md). Passing it requires a fresh owner
decision; no Zillow setting, Apify token, actor credential or rollout
attestation can enable a different site.
