BEGIN IMMEDIATE;

ALTER TABLE outbox ADD COLUMN last_error_class TEXT;
ALTER TABLE outbox ADD COLUMN operator_alerted_at TEXT;
ALTER TABLE outbox ADD COLUMN updated_at TEXT;
UPDATE outbox SET updated_at = created_at WHERE updated_at IS NULL;

CREATE INDEX idx_outbox_stale_sending ON outbox(status, claimed_at);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (5, 'canary delivery worker', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
