-- Quant Foundation v2: raw-data manifests and complete PIT trading calendars.
CREATE TABLE IF NOT EXISTS quant_data_artifact (
    artifact_id            TEXT PRIMARY KEY CHECK (length(artifact_id) = 64),
    data_kind              TEXT NOT NULL,
    data_format            TEXT NOT NULL,
    market                 TEXT CHECK (market IS NULL OR market IN ('A', 'HK', 'US')),
    source                 TEXT NOT NULL,
    source_dataset         TEXT NOT NULL,
    storage_key            TEXT NOT NULL,
    sha256                 TEXT NOT NULL CHECK (length(sha256) = 64),
    byte_size              INTEGER NOT NULL CHECK (byte_size >= 0),
    row_count              INTEGER CHECK (row_count IS NULL OR row_count >= 0),
    content_start          TEXT,
    content_end            TEXT,
    retrieved_at           TEXT NOT NULL,
    provider_version       TEXT NOT NULL,
    schema_version         TEXT NOT NULL,
    adapter_version        TEXT NOT NULL,
    known_at_policy        TEXT NOT NULL,
    revision_policy        TEXT NOT NULL,
    verified               INTEGER NOT NULL CHECK (verified IN (0, 1)),
    source_note            TEXT NOT NULL,
    calendar_snapshot_id   TEXT CHECK (
        calendar_snapshot_id IS NULL OR length(calendar_snapshot_id) = 64
    ),
    payload_json           TEXT NOT NULL,
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quant_data_artifact_storage
    ON quant_data_artifact(storage_key, sha256);
CREATE INDEX IF NOT EXISTS idx_quant_data_artifact_provenance
    ON quant_data_artifact(data_kind, market, source, retrieved_at);

CREATE TABLE IF NOT EXISTS quant_data_snapshot (
    snapshot_id                    TEXT PRIMARY KEY CHECK (length(snapshot_id) = 64),
    name                           TEXT NOT NULL,
    as_of                          TEXT NOT NULL,
    created_at                     TEXT NOT NULL,
    config_hash                    TEXT NOT NULL CHECK (length(config_hash) = 64),
    code_version                   TEXT NOT NULL,
    universe_snapshot_id           TEXT CHECK (
        universe_snapshot_id IS NULL OR length(universe_snapshot_id) = 64
    ),
    require_verified               INTEGER NOT NULL CHECK (require_verified IN (0, 1)),
    require_calendar_market_data   INTEGER NOT NULL CHECK (
        require_calendar_market_data IN (0, 1)
    ),
    require_universe_market_data   INTEGER NOT NULL CHECK (
        require_universe_market_data IN (0, 1)
    ),
    payload_json                   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quant_data_snapshot_time
    ON quant_data_snapshot(as_of, created_at);

CREATE TABLE IF NOT EXISTS quant_data_snapshot_artifact (
    snapshot_id    TEXT NOT NULL,
    artifact_id    TEXT NOT NULL,
    ordinal        INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (snapshot_id, artifact_id),
    UNIQUE (snapshot_id, ordinal),
    FOREIGN KEY (snapshot_id) REFERENCES quant_data_snapshot(snapshot_id),
    FOREIGN KEY (artifact_id) REFERENCES quant_data_artifact(artifact_id)
);

CREATE TABLE IF NOT EXISTS quant_calendar_coverage (
    coverage_id       TEXT PRIMARY KEY CHECK (length(coverage_id) = 64),
    market            TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    start_date        TEXT NOT NULL,
    end_date          TEXT NOT NULL,
    source            TEXT NOT NULL,
    calendar_version  TEXT NOT NULL,
    known_at          TEXT NOT NULL,
    revision_kind     TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value    TEXT NOT NULL,
    verified          INTEGER NOT NULL CHECK (verified IN (0, 1)),
    source_note       TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    CHECK (end_date >= start_date),
    CHECK (
        (revision_kind = 'STRING' AND length(revision_value) > 0)
        OR
        (
            revision_kind = 'INTEGER'
            AND printf('%d', CAST(revision_value AS INTEGER)) = revision_value
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_quant_calendar_coverage_lookup
    ON quant_calendar_coverage(market, start_date, end_date, known_at);

CREATE TABLE IF NOT EXISTS quant_calendar_day (
    day_id             TEXT PRIMARY KEY CHECK (length(day_id) = 64),
    coverage_id        TEXT NOT NULL,
    market             TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    session_date       TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    open_time          TEXT,
    close_time         TEXT,
    session_kind       TEXT NOT NULL CHECK (
        session_kind IN ('REGULAR', 'HALF_DAY', 'SPECIAL')
    ),
    known_at           TEXT NOT NULL,
    source             TEXT NOT NULL,
    calendar_version   TEXT NOT NULL,
    revision_kind      TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value     TEXT NOT NULL,
    verified           INTEGER NOT NULL CHECK (verified IN (0, 1)),
    source_note        TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    FOREIGN KEY (coverage_id) REFERENCES quant_calendar_coverage(coverage_id),
    CHECK (
        (status = 'OPEN' AND open_time IS NOT NULL AND close_time IS NOT NULL)
        OR
        (status = 'CLOSED' AND open_time IS NULL AND close_time IS NULL)
    ),
    CHECK (
        (revision_kind = 'STRING' AND length(revision_value) > 0)
        OR
        (
            revision_kind = 'INTEGER'
            AND printf('%d', CAST(revision_value AS INTEGER)) = revision_value
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_quant_calendar_day_lookup
    ON quant_calendar_day(market, session_date, known_at);

CREATE TABLE IF NOT EXISTS quant_instrument_session_status (
    status_id          TEXT PRIMARY KEY CHECK (length(status_id) = 64),
    symbol             TEXT NOT NULL,
    market             TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    session_date       TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (
        status IN ('SUSPENDED', 'HALTED', 'VCM_HALT', 'DELISTED', 'UNKNOWN')
    ),
    known_at           TEXT NOT NULL,
    source             TEXT NOT NULL,
    revision_kind      TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value     TEXT NOT NULL,
    reference_price    REAL CHECK (reference_price IS NULL OR reference_price > 0),
    share_factor       REAL NOT NULL CHECK (share_factor > 0),
    verified           INTEGER NOT NULL CHECK (verified IN (0, 1)),
    source_note        TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    CHECK (
        (revision_kind = 'STRING' AND length(revision_value) > 0)
        OR
        (
            revision_kind = 'INTEGER'
            AND printf('%d', CAST(revision_value AS INTEGER)) = revision_value
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_quant_instrument_session_status_lookup
    ON quant_instrument_session_status(symbol, session_date, known_at);

CREATE TRIGGER IF NOT EXISTS quant_data_artifact_no_update
BEFORE UPDATE ON quant_data_artifact
BEGIN
    SELECT RAISE(ABORT, 'quant_data_artifact is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_data_artifact_no_delete
BEFORE DELETE ON quant_data_artifact
BEGIN
    SELECT RAISE(ABORT, 'quant_data_artifact is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_data_snapshot_no_update
BEFORE UPDATE ON quant_data_snapshot
BEGIN
    SELECT RAISE(ABORT, 'quant_data_snapshot is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_data_snapshot_no_delete
BEFORE DELETE ON quant_data_snapshot
BEGIN
    SELECT RAISE(ABORT, 'quant_data_snapshot is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_data_snapshot_artifact_no_update
BEFORE UPDATE ON quant_data_snapshot_artifact
BEGIN
    SELECT RAISE(ABORT, 'quant_data_snapshot_artifact is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_data_snapshot_artifact_no_delete
BEFORE DELETE ON quant_data_snapshot_artifact
BEGIN
    SELECT RAISE(ABORT, 'quant_data_snapshot_artifact is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_calendar_coverage_no_update
BEFORE UPDATE ON quant_calendar_coverage
BEGIN
    SELECT RAISE(ABORT, 'quant_calendar_coverage is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_calendar_coverage_no_delete
BEFORE DELETE ON quant_calendar_coverage
BEGIN
    SELECT RAISE(ABORT, 'quant_calendar_coverage is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_calendar_day_no_update
BEFORE UPDATE ON quant_calendar_day
BEGIN
    SELECT RAISE(ABORT, 'quant_calendar_day is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_calendar_day_no_delete
BEFORE DELETE ON quant_calendar_day
BEGIN
    SELECT RAISE(ABORT, 'quant_calendar_day is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_instrument_session_status_no_update
BEFORE UPDATE ON quant_instrument_session_status
BEGIN
    SELECT RAISE(ABORT, 'quant_instrument_session_status is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_instrument_session_status_no_delete
BEFORE DELETE ON quant_instrument_session_status
BEGIN
    SELECT RAISE(ABORT, 'quant_instrument_session_status is append-only');
END;
