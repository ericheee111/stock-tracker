CREATE TABLE runtime_evidence_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TRIGGER runtime_evidence_meta_no_update
BEFORE UPDATE ON runtime_evidence_meta
BEGIN
    SELECT RAISE(ABORT, 'runtime_evidence_meta is append-only');
END;
CREATE TRIGGER runtime_evidence_meta_no_delete
BEFORE DELETE ON runtime_evidence_meta
BEGIN
    SELECT RAISE(ABORT, 'runtime_evidence_meta is append-only');
END;
CREATE TABLE runtime_transition_occurrence (
    occurrence_order INTEGER PRIMARY KEY CHECK(occurrence_order > 0),
    occurrence_dedup_id TEXT NOT NULL UNIQUE,
    decision_content_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL UNIQUE,
    transition_event_id TEXT NOT NULL UNIQUE,
    upstream_occurrence_id TEXT,
    signal_history_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES runtime_transition_outbox(artifact_id),
    FOREIGN KEY(signal_history_id) REFERENCES signal_history(id)
);
CREATE INDEX idx_runtime_occurrence_decision
ON runtime_transition_occurrence(decision_content_id, occurrence_order);
CREATE TRIGGER runtime_transition_occurrence_no_update
BEFORE UPDATE ON runtime_transition_occurrence
BEGIN
    SELECT RAISE(ABORT, 'runtime_transition_occurrence is append-only');
END;
CREATE TRIGGER runtime_transition_occurrence_no_delete
BEFORE DELETE ON runtime_transition_occurrence
BEGIN
    SELECT RAISE(ABORT, 'runtime_transition_occurrence is append-only');
END;
CREATE TABLE runtime_worker_integrity_block (
    block_order INTEGER PRIMARY KEY CHECK(block_order > 0),
    block_id TEXT NOT NULL UNIQUE,
    worker_id TEXT NOT NULL UNIQUE,
    artifact_id TEXT NOT NULL,
    block_code TEXT NOT NULL,
    blocked_at TEXT NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES runtime_transition_outbox(artifact_id)
);
CREATE TRIGGER runtime_worker_integrity_block_no_update
BEFORE UPDATE ON runtime_worker_integrity_block
BEGIN
    SELECT RAISE(ABORT, 'runtime_worker_integrity_block is append-only');
END;
CREATE TRIGGER runtime_worker_integrity_block_no_delete
BEFORE DELETE ON runtime_worker_integrity_block
BEGIN
    SELECT RAISE(ABORT, 'runtime_worker_integrity_block is append-only');
END;
