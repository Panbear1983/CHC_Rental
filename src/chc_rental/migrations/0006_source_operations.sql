BEGIN IMMEDIATE;

CREATE TABLE source_breakers (
    source TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'closed'
        CHECK (state IN ('closed', 'open', 'half_open')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    last_error_class TEXT,
    last_error_message TEXT,
    opened_at TEXT,
    retry_at TEXT,
    last_success_at TEXT,
    pending_alert TEXT
        CHECK (pending_alert IS NULL OR pending_alert IN (
            'failure_started', 'opened', 'escalated', 'recovered'
        )),
    failure_alerted_at TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (6, 'source operations and breaker', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
