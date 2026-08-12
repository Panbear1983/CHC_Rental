# Incremental Zillow alerts — operator runbook

The incremental path is implemented but ships paused. It is additive: the
existing `com.chcrental.daily` job and `chc-rental run` command remain the
rollback path. The separate `com.chcrental.alerts` LaunchAgent is not installed
or loaded by the build.

## Safety model

A Zillow result can reach Telegram only when every gate is true:

1. `live_push_enabled` is on;
2. `incremental_alerts_enabled` is on;
3. `zillow_enabled` is on and `APIFY_TOKEN` is usable;
4. the recipient is active, is listed in `incremental_canary_telegram_ids`, and
   uses `delivery_mode: immediate`.

The worker rechecks the allowlist, current search filters, legacy seen ledger,
and quiet hours immediately before transport. The first usable result for each
source-query fingerprint is always a silent baseline.

Telegram has no caller-supplied idempotency key. A timeout or restart after the
local row enters `sending` therefore becomes `uncertain`; it is displayed for
manual review and is never automatically retried. Only definite refusals use
bounded exponential backoff. Chat/auth rejections become terminal `failed`
rows.

## Prepare and inspect

```bash
./chc.sh alerts migrate --check
./chc.sh alerts migrate --apply
./chc.sh alerts status --json
./chc.sh alerts deliver                 # read-only outbox counts
```

The dashboard's Alerts screen shows query baselines, last success/staleness,
Apify run IDs, errors, truncation, source breaker state, costs and outbox state.
Its source settings write the same YAML consumed by the scheduler. A token is
shown only as present/missing; the value is never rendered.

## Shadow validation

An offline fixture tick writes baseline/shadow state to the selected root but
does not meter a request and cannot send. Use a JSON list of actor items, and do
not run it against a ledger whose real baseline you intend to preserve:

```bash
./chc.sh alerts tick --fixture <APIFY_ITEMS_LIST.json>
```

A live source-only cycle may consume one bounded Apify start but never calls
Telegram:

```bash
./chc.sh alerts cycle --shadow
```

Before any canary message, establish a baseline, replay it to confirm zero
outbox additions, then introduce or wait for one genuinely new match and inspect
the exact `shadow` message in the dashboard.

## One controlled Peter canary

Add only Peter's already-allowlisted Telegram ID to the canary field, set his
delivery mode to Immediate, and keep every other profile in Daily mode. Select
the exact shadow row in the dashboard, or use the explicit CLI boundary:

```bash
./chc.sh alerts deliver --live \
  --confirm-telegram-id <PETER_TELEGRAM_ID> \
  --outbox-id <SHADOW_OUTBOX_ID> \
  --max-messages 1
```

Confirm the recipient, search name, price, listing URL and durable Telegram
receipt. Replay the same observation and confirm no second message. Do not add
another canary until Peter's 48-hour observation window has no unexplained
duplicate, cost breach or false no-results behavior.

## Cost and breaker behavior

Hard controls apply before every new actor start: per-run charge cap, Zillow
daily start ceiling and global daily ceiling. When configured, the monthly soft
budget blocks new starts once known recorded cost reaches it; already running
actors and queued Telegram delivery continue to recover. Unknown run charges
are surfaced as `cost_unknown` and prevent any claim that cadence can be safely
increased.

The dashboard projection uses active scopes, configured cadence, the daily cap
and observed p95 cost. It remains `unknown` until at least one run has a known
charge.

Auth failures open the persisted Zillow circuit breaker immediately. Other
source/payload failures open it after the configured consecutive-failure
threshold. After cooldown exactly one half-open probe is permitted; success
closes the breaker and records recovery. Owner alerts occur once when a failure
streak begins, again if the breaker opens or the error class meaningfully
changes, and once on recovery.

## Backup and recovery

Each scheduler tick creates or verifies one consistent SQLite backup per UTC
day. The restore drill copies a backup into a disposable database, verifies its
schema and `PRAGMA integrity_check`, and never replaces live state.

```bash
./chc.sh alerts backup
./chc.sh alerts backup --verify backups/alerts-daily-YYYY-MM-DD.sqlite3
./chc.sh alerts backup --drill backups/alerts-daily-YYYY-MM-DD.sqlite3
```

On restart, the next tick resumes persisted Apify run IDs, re-reads successful
but unprocessed datasets, releases expired query leases, and quarantines stale
`sending` rows as `uncertain`. The whole-run lock prevents the daily job, a
manual shadow run and the alerts job from overlapping.

Completed source runs, sent/cancelled outbox rows and their receipts are kept
for `incremental_event_retention_days` (180 days by default). Active query
scopes plus pending, retrying, failed and uncertain delivery rows are retained.
Raw daily source caches continue to use their shorter independent retention.

## Scheduler activation and rollback

The new job is separate and disabled by default. Only after the live canary
gate passes:

```bash
cp scripts/com.chcrental.alerts.plist ~/Library/LaunchAgents/
./chc.sh alerts-job start
./chc.sh alerts-job status
./chc.sh alerts-logs -f
```

Immediate rollback does not touch the daily workflow:

```bash
./chc.sh alerts-job stop
# Then turn incremental_alerts_enabled off in the dashboard.
```

Do not install the alerts plist merely to test it. `./chc.sh alerts tick
--fixture ...` and the dashboard shadow action cover the pre-activation path.
