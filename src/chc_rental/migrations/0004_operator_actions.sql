BEGIN IMMEDIATE;

CREATE TABLE operator_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_operator_audit_created ON operator_audit(created_at, audit_id);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (4, 'audited operator actions', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
