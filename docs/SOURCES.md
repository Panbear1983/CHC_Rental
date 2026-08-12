# Listing sources — vetting record (Phase 0)

Required by the buildout plan: no adapter may be activated for a source without
an explicit status here. Scope decision (Peter, 2026-08-11): **US rentals only.**

## RentCast — ALLOWED-WITH-LIMITS

| | |
|---|---|
| Status | **ALLOWED-WITH-LIMITS** (official commercial API; per-plan request limits) |
| Checked | 2026-08-11 |
| Access | REST API, `X-Api-Key` header; key already provisioned in `.env` (`RENTCAST_API_KEY`) |
| API docs | https://developers.rentcast.io/reference/introduction |
| Terms | https://www.rentcast.io/terms-of-use (API access is the product being sold; programmatic use with a key is the licensed path) |
| robots.txt | Not applicable — this is the vendor's API, not a crawl of their site |
| Endpoint used | `GET /v1/listings/rental/long-term` (active long-term rental listings by city/state) |
| Local ceilings | `config/settings.yaml`: 50 requests/day for this source, 100/day global; hard stop + operator alert at the ceiling |

Known data-shape limits that constrain product behavior:

- Responses carry **no consumer-facing listing URL**. Pushes link to a Google
  Maps search for the listing address instead.
- **No feature/amenity list** on this endpoint. A search with
  `required_features` set will never match a RentCast listing — leave features
  empty for RentCast-backed searches.
- **No district/neighborhood field.** Leave `district` blank for a true
  neighborhood filter. The matcher permits only one safe fallback: when the
  requested district exactly equals the listing city (for example,
  Brooklyn/Brooklyn).
- `state` (2-letter) is required on every search for query planning.

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
| Local ceilings | Disabled by default; 5 actor runs/day; 25 results/city; USD 0.25 maximum charge/run; 100 requests/day global |
| Kill switch | `zillow_enabled: false`, missing token, zero results, or zero request budget |

Implementation boundaries:

- CHC_Rental never directly requests Zillow pages and contains no CAPTCHA,
  proxy, login or anti-bot bypass code. The managed actor receives a rental
  search URL and returns a bounded dataset.
- The actor requires a map-backed search URL. CHC resolves US city/state bounds
  through Nominatim with an identifying user agent and caches the resulting
  Zillow pool for the UTC day, so the hourly job does not repeat the lookup.
- Only explicit rental results are admitted. Explicit sale/sold results are
  discarded. Actor control records and building-summary cards without exact
  unit-level bath/price data are skipped rather than fabricated; other
  malformed records go through the normal rejected-record path.
- Zillow failure is isolated from RentCast. Positive matches from a usable
  source may still deliver, but no-results notices are suppressed until every
  enabled source produced a usable pool.
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
