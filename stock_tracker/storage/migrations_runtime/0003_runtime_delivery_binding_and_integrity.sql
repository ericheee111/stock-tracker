ALTER TABLE runtime_transition_occurrence
ADD COLUMN signal_history_sha256 TEXT NOT NULL DEFAULT ''
CHECK(
    length(signal_history_sha256) = 64
    AND signal_history_sha256 NOT GLOB '*[^0-9a-f]*'
);
ALTER TABLE runtime_transition_occurrence
ADD COLUMN occurrence_append_order INTEGER NOT NULL DEFAULT 0
CHECK(occurrence_append_order > 0);
ALTER TABLE runtime_transition_occurrence
ADD COLUMN previous_occurrence_hash TEXT NOT NULL DEFAULT ''
CHECK(
    length(previous_occurrence_hash) = 64
    AND previous_occurrence_hash NOT GLOB '*[^0-9a-f]*'
);
ALTER TABLE runtime_transition_occurrence
ADD COLUMN occurrence_hash TEXT NOT NULL DEFAULT ''
CHECK(
    length(occurrence_hash) = 64
    AND occurrence_hash NOT GLOB '*[^0-9a-f]*'
);
CREATE UNIQUE INDEX idx_runtime_occurrence_append_order
ON runtime_transition_occurrence(occurrence_append_order);

ALTER TABLE runtime_outbox_delivery
ADD COLUMN delivered_artifact_store_id TEXT
CHECK(
    delivered_artifact_store_id IS NULL
    OR (
        length(delivered_artifact_store_id) = 64
        AND delivered_artifact_store_id NOT GLOB '*[^0-9a-f]*'
    )
);
ALTER TABLE runtime_outbox_delivery
ADD COLUMN delivered_append_order INTEGER
CHECK(delivered_append_order IS NULL OR delivered_append_order > 0);

CREATE TABLE runtime_artifact_delivery_binding (
    binding_order INTEGER PRIMARY KEY CHECK(binding_order = 1),
    delivery_binding_id TEXT NOT NULL UNIQUE,
    runtime_store_id TEXT NOT NULL UNIQUE,
    artifact_store_id TEXT NOT NULL UNIQUE,
    bound_at TEXT NOT NULL
);
CREATE TRIGGER runtime_artifact_delivery_binding_no_update
BEFORE UPDATE ON runtime_artifact_delivery_binding
BEGIN
    SELECT RAISE(ABORT, 'runtime_artifact_delivery_binding is append-only');
END;
CREATE TRIGGER runtime_artifact_delivery_binding_no_delete
BEFORE DELETE ON runtime_artifact_delivery_binding
BEGIN
    SELECT RAISE(ABORT, 'runtime_artifact_delivery_binding is append-only');
END;

CREATE TABLE runtime_artifact_integrity_event (
    event_order INTEGER PRIMARY KEY CHECK(event_order > 0),
    integrity_event_id TEXT NOT NULL UNIQUE,
    delivery_binding_id TEXT NOT NULL,
    artifact_id TEXT,
    worker_id TEXT NOT NULL,
    block_code TEXT NOT NULL,
    blocked_at TEXT NOT NULL,
    FOREIGN KEY(delivery_binding_id)
        REFERENCES runtime_artifact_delivery_binding(delivery_binding_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(artifact_id)
        REFERENCES runtime_transition_outbox(artifact_id)
        ON DELETE RESTRICT
);
CREATE TRIGGER runtime_artifact_integrity_event_no_update
BEFORE UPDATE ON runtime_artifact_integrity_event
BEGIN
    SELECT RAISE(ABORT, 'runtime_artifact_integrity_event is append-only');
END;
CREATE TRIGGER runtime_artifact_integrity_event_no_delete
BEFORE DELETE ON runtime_artifact_integrity_event
BEGIN
    SELECT RAISE(ABORT, 'runtime_artifact_integrity_event is append-only');
END;

CREATE TRIGGER runtime_bound_signal_history_no_update
BEFORE UPDATE ON signal_history
WHEN EXISTS (
    SELECT 1 FROM runtime_transition_occurrence
    WHERE signal_history_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'runtime-bound signal_history is immutable');
END;
CREATE TRIGGER runtime_bound_signal_history_no_delete
BEFORE DELETE ON signal_history
WHEN EXISTS (
    SELECT 1 FROM runtime_transition_occurrence
    WHERE signal_history_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'runtime-bound signal_history is immutable');
END;
