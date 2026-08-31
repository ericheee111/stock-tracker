CREATE TABLE runtime_schema_migration (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TRIGGER runtime_schema_migration_no_update
BEFORE UPDATE ON runtime_schema_migration
BEGIN
    SELECT RAISE(ABORT, 'runtime_schema_migration is append-only');
END;
CREATE TRIGGER runtime_schema_migration_no_delete
BEFORE DELETE ON runtime_schema_migration
BEGIN
    SELECT RAISE(ABORT, 'runtime_schema_migration is append-only');
END;
CREATE TABLE runtime_transition_outbox (
    append_order INTEGER PRIMARY KEY CHECK(append_order > 0),
    artifact_id TEXT NOT NULL UNIQUE,
    runtime_signal_id TEXT NOT NULL,
    transition_event_id TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_runtime_transition_signal
ON runtime_transition_outbox(runtime_signal_id, append_order);
CREATE TRIGGER runtime_transition_outbox_no_update
BEFORE UPDATE ON runtime_transition_outbox
BEGIN
    SELECT RAISE(ABORT, 'runtime_transition_outbox payload is immutable');
END;
CREATE TRIGGER runtime_transition_outbox_no_delete
BEFORE DELETE ON runtime_transition_outbox
BEGIN
    SELECT RAISE(ABORT, 'runtime_transition_outbox payload is immutable');
END;
CREATE TABLE runtime_outbox_delivery (
    artifact_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('PENDING','LEASED','DELIVERED','QUARANTINED')),
    lease_owner TEXT,
    lease_expires_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    next_retry_at TEXT,
    last_error_code TEXT,
    delivered_record_hash TEXT,
    delivered_audit_id TEXT,
    delivered_at TEXT,
    FOREIGN KEY(artifact_id) REFERENCES runtime_transition_outbox(artifact_id)
);
CREATE INDEX idx_runtime_outbox_delivery_ready
ON runtime_outbox_delivery(status, next_retry_at, lease_expires_at);
CREATE TABLE runtime_outbox_cursor (
    worker_id TEXT PRIMARY KEY,
    last_contiguous_order INTEGER NOT NULL DEFAULT 0 CHECK(last_contiguous_order >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE runtime_outbox_quarantine (
    quarantine_order INTEGER PRIMARY KEY CHECK(quarantine_order > 0),
    quarantine_id TEXT NOT NULL UNIQUE,
    artifact_id TEXT,
    outbox_append_order INTEGER,
    runtime_signal_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    quarantined_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    metadata_sha256 TEXT NOT NULL
);
CREATE INDEX idx_runtime_outbox_quarantine_artifact
ON runtime_outbox_quarantine(artifact_id, outbox_append_order);
CREATE TRIGGER runtime_outbox_quarantine_no_update
BEFORE UPDATE ON runtime_outbox_quarantine
BEGIN
    SELECT RAISE(ABORT, 'runtime_outbox_quarantine is append-only');
END;
CREATE TRIGGER runtime_outbox_quarantine_no_delete
BEFORE DELETE ON runtime_outbox_quarantine
BEGIN
    SELECT RAISE(ABORT, 'runtime_outbox_quarantine is append-only');
END;
