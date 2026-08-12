BEGIN IMMEDIATE;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE query_scopes (
    query_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    query_json TEXT NOT NULL,
    baseline_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (baseline_state IN ('pending', 'established', 'reset_pending')),
    baseline_established_at TEXT,
    last_due_at TEXT,
    last_success_at TEXT,
    next_due_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_query_scopes_due ON query_scopes(source, next_due_at);

CREATE TABLE source_runs (
    run_id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL REFERENCES query_scopes(query_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    apify_run_id TEXT UNIQUE,
    status TEXT NOT NULL
        CHECK (status IN ('reserved', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled')),
    input_fingerprint TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    result_count INTEGER CHECK (result_count IS NULL OR result_count >= 0),
    truncated INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
    coverage_json TEXT,
    error_class TEXT,
    error_message TEXT,
    charge_usd REAL CHECK (charge_usd IS NULL OR charge_usd >= 0),
    charge_known INTEGER NOT NULL DEFAULT 0 CHECK (charge_known IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_source_runs_status ON source_runs(status, started_at);
CREATE INDEX idx_source_runs_query ON source_runs(query_id, started_at);

CREATE TABLE listing_identities (
    identity_key TEXT PRIMARY KEY,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL
);

CREATE TABLE listing_versions (
    identity_key TEXT NOT NULL REFERENCES listing_identities(identity_key) ON DELETE CASCADE,
    version_hash TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    source_posted_at TEXT,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (identity_key, version_hash)
);

CREATE TABLE observations (
    run_id TEXT NOT NULL REFERENCES source_runs(run_id) ON DELETE CASCADE,
    identity_key TEXT NOT NULL,
    version_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    source_listing_id TEXT,
    source_url TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, identity_key, version_hash),
    FOREIGN KEY (identity_key, version_hash)
        REFERENCES listing_versions(identity_key, version_hash) ON DELETE CASCADE
);
CREATE INDEX idx_observations_identity ON observations(identity_key, observed_at);

CREATE TABLE outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
    search_id TEXT NOT NULL,
    primary_search_name TEXT NOT NULL,
    matching_search_ids_json TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    version_hash TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('new', 'price_change', 'material_change', 'repost_candidate')),
    message_text TEXT NOT NULL DEFAULT '',
    not_before TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'shadow'
        CHECK (status IN ('shadow', 'pending', 'sending', 'sent', 'failed', 'retry_wait', 'uncertain', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    sent_at TEXT,
    FOREIGN KEY (identity_key, version_hash)
        REFERENCES listing_versions(identity_key, version_hash) ON DELETE RESTRICT
);
CREATE INDEX idx_outbox_claim ON outbox(status, not_before, outbox_id);
CREATE INDEX idx_outbox_recipient ON outbox(telegram_id, created_at);

CREATE TABLE delivery_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id INTEGER NOT NULL UNIQUE REFERENCES outbox(outbox_id) ON DELETE RESTRICT,
    telegram_message_id TEXT,
    accepted_at TEXT NOT NULL
);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (1, 'alert ledger foundation', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
