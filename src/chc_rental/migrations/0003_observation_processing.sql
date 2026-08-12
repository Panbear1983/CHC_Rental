BEGIN IMMEDIATE;

ALTER TABLE source_runs ADD COLUMN processed_at TEXT;

CREATE TABLE listing_events (
    run_id TEXT NOT NULL REFERENCES source_runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL REFERENCES query_scopes(query_id) ON DELETE CASCADE,
    identity_key TEXT NOT NULL,
    version_hash TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('baseline', 'new', 'price_change', 'material_change', 'duplicate')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, identity_key, version_hash),
    FOREIGN KEY (identity_key, version_hash)
        REFERENCES listing_versions(identity_key, version_hash) ON DELETE CASCADE
);
CREATE INDEX idx_listing_events_query ON listing_events(query_id, created_at);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (3, 'durable observation processing', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
