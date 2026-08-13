-- Quant Foundation v1: PIT, labels, evaluation and model-governance evidence.
CREATE TABLE IF NOT EXISTS quant_schema_migration (
    version         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    checksum        TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_at      TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS quant_schema_migration_no_update
BEFORE UPDATE ON quant_schema_migration
BEGIN
    SELECT RAISE(ABORT, 'quant_schema_migration is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_schema_migration_no_delete
BEFORE DELETE ON quant_schema_migration
BEGIN
    SELECT RAISE(ABORT, 'quant_schema_migration is append-only');
END;

CREATE TABLE IF NOT EXISTS pit_fact (
    fact_id          TEXT PRIMARY KEY CHECK (length(fact_id) = 64),
    namespace        TEXT NOT NULL,
    entity_id        TEXT NOT NULL,
    field_name       TEXT NOT NULL,
    event_time       TEXT NOT NULL,
    known_at         TEXT NOT NULL,
    usable_from      TEXT NOT NULL,
    revision_kind    TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value   TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    source           TEXT NOT NULL,
    verified         INTEGER NOT NULL CHECK (verified IN (0, 1)),
    created_at       TEXT NOT NULL,
    CHECK (
        (revision_kind = 'STRING' AND length(revision_value) > 0)
        OR
        (
            revision_kind = 'INTEGER'
            AND printf('%d', CAST(revision_value AS INTEGER)) = revision_value
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_pit_fact_lookup
    ON pit_fact(namespace, entity_id, field_name, event_time, known_at);
CREATE INDEX IF NOT EXISTS idx_pit_fact_availability
    ON pit_fact(known_at, usable_from, verified);

CREATE TABLE IF NOT EXISTS quant_label (
    label_id             TEXT PRIMARY KEY CHECK (length(label_id) = 64),
    symbol               TEXT NOT NULL,
    market               TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    signal_time          TEXT NOT NULL,
    label_start_time     TEXT NOT NULL,
    label_end_time       TEXT NOT NULL,
    label                INTEGER NOT NULL CHECK (label IN (-1, 0, 1)),
    outcome              TEXT NOT NULL,
    entry_price          REAL,
    exit_price           REAL,
    target_price         REAL,
    stop_price           REAL,
    calendar_snapshot_id TEXT NOT NULL CHECK (length(calendar_snapshot_id) = 64),
    market_rule_hash     TEXT NOT NULL CHECK (length(market_rule_hash) = 64),
    cost_schedule_hash   TEXT NOT NULL CHECK (length(cost_schedule_hash) = 64),
    feature_set_id       TEXT CHECK (feature_set_id IS NULL OR length(feature_set_id) = 64),
    label_version        TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quant_label_symbol_time
    ON quant_label(symbol, signal_time, label_end_time);
CREATE INDEX IF NOT EXISTS idx_quant_label_market_outcome
    ON quant_label(market, outcome, label_end_time);

CREATE TABLE IF NOT EXISTS quant_model_registry_event (
    event_id          TEXT PRIMARY KEY CHECK (length(event_id) = 64),
    event_type        TEXT NOT NULL CHECK (
        event_type IN ('REGISTER', 'PROMOTE', 'REJECT', 'RETIRE')
    ),
    model_id          TEXT NOT NULL,
    strategy_id       TEXT NOT NULL,
    market            TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    horizon_sessions  INTEGER NOT NULL CHECK (horizon_sessions > 0),
    occurred_at       TEXT NOT NULL,
    comparison_id     TEXT CHECK (comparison_id IS NULL OR length(comparison_id) = 64),
    evidence_id       TEXT NOT NULL CHECK (length(evidence_id) = 64),
    payload_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quant_model_registry_stream
    ON quant_model_registry_event(strategy_id, market, horizon_sessions, occurred_at);

CREATE TABLE IF NOT EXISTS quant_experiment_event (
    event_id             TEXT PRIMARY KEY CHECK (length(event_id) = 64),
    experiment_id        TEXT NOT NULL,
    event_type           TEXT NOT NULL CHECK (
        event_type IN ('CREATED', 'STARTED', 'COMPLETED', 'FAILED', 'REJECTED')
    ),
    occurred_at          TEXT NOT NULL,
    reproducibility_id   TEXT NOT NULL CHECK (length(reproducibility_id) = 64),
    comparison_id        TEXT CHECK (comparison_id IS NULL OR length(comparison_id) = 64),
    payload_json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quant_experiment_stream
    ON quant_experiment_event(experiment_id, occurred_at);

CREATE TABLE IF NOT EXISTS quant_holdout (
    record_id          TEXT PRIMARY KEY CHECK (length(record_id) = 64),
    holdout_id         TEXT NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('SEALED', 'EXPOSED', 'COMPROMISED')),
    config_hash        TEXT NOT NULL CHECK (length(config_hash) = 64),
    data_snapshot_id   TEXT NOT NULL CHECK (length(data_snapshot_id) = 64),
    occurred_at        TEXT NOT NULL,
    payload_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quant_holdout_stream
    ON quant_holdout(holdout_id, occurred_at);

CREATE TRIGGER IF NOT EXISTS pit_fact_no_update
BEFORE UPDATE ON pit_fact
BEGIN
    SELECT RAISE(ABORT, 'pit_fact is append-only');
END;
CREATE TRIGGER IF NOT EXISTS pit_fact_no_delete
BEFORE DELETE ON pit_fact
BEGIN
    SELECT RAISE(ABORT, 'pit_fact is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_label_no_update
BEFORE UPDATE ON quant_label
BEGIN
    SELECT RAISE(ABORT, 'quant_label is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_label_no_delete
BEFORE DELETE ON quant_label
BEGIN
    SELECT RAISE(ABORT, 'quant_label is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_model_registry_event_no_update
BEFORE UPDATE ON quant_model_registry_event
BEGIN
    SELECT RAISE(ABORT, 'quant_model_registry_event is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_model_registry_event_no_delete
BEFORE DELETE ON quant_model_registry_event
BEGIN
    SELECT RAISE(ABORT, 'quant_model_registry_event is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_experiment_event_no_update
BEFORE UPDATE ON quant_experiment_event
BEGIN
    SELECT RAISE(ABORT, 'quant_experiment_event is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_experiment_event_no_delete
BEFORE DELETE ON quant_experiment_event
BEGIN
    SELECT RAISE(ABORT, 'quant_experiment_event is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_holdout_no_update
BEFORE UPDATE ON quant_holdout
BEGIN
    SELECT RAISE(ABORT, 'quant_holdout is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_holdout_no_delete
BEFORE DELETE ON quant_holdout
BEGIN
    SELECT RAISE(ABORT, 'quant_holdout is append-only');
END;
