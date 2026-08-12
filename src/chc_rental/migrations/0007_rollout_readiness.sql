BEGIN IMMEDIATE;

ALTER TABLE source_runs ADD COLUMN execution_mode TEXT NOT NULL
    DEFAULT 'legacy_unknown'
    CHECK (execution_mode IN ('live', 'fixture', 'legacy_unknown'));

CREATE TABLE scheduler_ticks (
    tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL CHECK (mode IN ('live', 'fixture', 'test')),
    status TEXT NOT NULL CHECK (status IN ('ok', 'degraded')),
    source_started INTEGER NOT NULL DEFAULT 0 CHECK (source_started >= 0),
    source_succeeded INTEGER NOT NULL DEFAULT 0 CHECK (source_succeeded >= 0),
    source_failed INTEGER NOT NULL DEFAULT 0 CHECK (source_failed >= 0),
    delivery_sent INTEGER NOT NULL DEFAULT 0 CHECK (delivery_sent >= 0),
    delivery_failed INTEGER NOT NULL DEFAULT 0 CHECK (delivery_failed >= 0),
    delivery_uncertain INTEGER NOT NULL DEFAULT 0 CHECK (delivery_uncertain >= 0),
    warning_count INTEGER NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_scheduler_ticks_mode_created
ON scheduler_ticks(mode, created_at);

CREATE TABLE rollout_attestations (
    gate TEXT PRIMARY KEY,
    evidence TEXT NOT NULL,
    attested_at TEXT NOT NULL
);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (7, 'rollout readiness evidence', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
