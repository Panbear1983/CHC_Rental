BEGIN IMMEDIATE;

ALTER TABLE query_scopes ADD COLUMN active INTEGER NOT NULL DEFAULT 1
    CHECK (active IN (0, 1));
ALTER TABLE query_scopes ADD COLUMN retired_at TEXT;
ALTER TABLE query_scopes ADD COLUMN lease_owner TEXT;
ALTER TABLE query_scopes ADD COLUMN lease_until TEXT;

ALTER TABLE source_runs ADD COLUMN default_dataset_id TEXT;
ALTER TABLE source_runs ADD COLUMN updated_at TEXT;
UPDATE source_runs SET updated_at = created_at WHERE updated_at IS NULL;

CREATE UNIQUE INDEX idx_source_runs_one_open_per_scope
ON source_runs(query_id)
WHERE status IN ('reserved', 'running');

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (2, 'incremental source run lifecycle', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
