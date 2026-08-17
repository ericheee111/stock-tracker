-- Stage 2A: append-only PIT security identity, daily status and historical universe.
-- Existing v2 calendar rows predate usable_from and therefore keep NULL; readers
-- must interpret NULL as known_at. New authoritative adapters must write explicit
-- UTC-normalized usable_from values.
ALTER TABLE quant_calendar_coverage ADD COLUMN usable_from TEXT;
ALTER TABLE quant_calendar_day ADD COLUMN usable_from TEXT;
ALTER TABLE quant_calendar_day ADD COLUMN supersedes_revision_kind TEXT CHECK (
    supersedes_revision_kind IS NULL
    OR supersedes_revision_kind IN ('INTEGER', 'STRING')
);
ALTER TABLE quant_calendar_day ADD COLUMN supersedes_revision_value TEXT;
ALTER TABLE quant_instrument_session_status ADD COLUMN usable_from TEXT;
CREATE INDEX IF NOT EXISTS idx_quant_calendar_coverage_visibility
    ON quant_calendar_coverage(market, start_date, end_date, known_at, usable_from);
CREATE INDEX IF NOT EXISTS idx_quant_calendar_day_visibility
    ON quant_calendar_day(market, session_date, known_at, usable_from);
CREATE INDEX IF NOT EXISTS idx_quant_instrument_session_status_visibility
    ON quant_instrument_session_status(symbol, session_date, known_at, usable_from);
CREATE TRIGGER IF NOT EXISTS quant_calendar_day_supersedes_revision_guard
BEFORE INSERT ON quant_calendar_day
WHEN
    ((NEW.supersedes_revision_kind IS NULL) <> (NEW.supersedes_revision_value IS NULL))
    OR (
        NEW.supersedes_revision_kind = 'STRING'
        AND length(NEW.supersedes_revision_value) = 0
    )
    OR (
        NEW.supersedes_revision_kind = 'INTEGER'
        AND printf('%d', CAST(NEW.supersedes_revision_value AS INTEGER))
            <> NEW.supersedes_revision_value
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid quant_calendar_day supersedes revision');
END;

CREATE TABLE IF NOT EXISTS quant_universe_coverage (
    coverage_id        TEXT PRIMARY KEY CHECK (length(coverage_id) = 64),
    universe_id        TEXT NOT NULL,
    market             TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    start_date         TEXT NOT NULL,
    end_date           TEXT NOT NULL,
    source             TEXT NOT NULL,
    universe_version   TEXT NOT NULL,
    known_at           TEXT NOT NULL,
    usable_from        TEXT NOT NULL,
    revision_kind      TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value     TEXT NOT NULL,
    verified           INTEGER NOT NULL CHECK (verified IN (0, 1)),
    complete           INTEGER NOT NULL CHECK (complete IN (0, 1)),
    source_note        TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    CHECK (length(universe_id) > 0),
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
CREATE INDEX IF NOT EXISTS idx_quant_universe_coverage_lookup
    ON quant_universe_coverage(
        universe_id, market, start_date, end_date, known_at, usable_from
    );

CREATE TABLE IF NOT EXISTS quant_instrument_identity (
    identity_id        TEXT PRIMARY KEY CHECK (length(identity_id) = 64),
    instrument_id      TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    market             TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    exchange           TEXT NOT NULL,
    security_type      TEXT NOT NULL CHECK (
        security_type IN (
            'COMMON_EQUITY', 'PREFERRED_EQUITY', 'ETF', 'FUND',
            'BOND', 'INDEX', 'OTHER'
        )
    ),
    effective_from     TEXT NOT NULL,
    effective_to       TEXT,
    known_at           TEXT NOT NULL,
    usable_from        TEXT NOT NULL,
    source             TEXT NOT NULL,
    revision_kind      TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value     TEXT NOT NULL,
    verified           INTEGER NOT NULL CHECK (verified IN (0, 1)),
    source_note        TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    CHECK (length(instrument_id) > 0),
    CHECK (length(symbol) > 0),
    CHECK (length(exchange) > 0),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK (
        (revision_kind = 'STRING' AND length(revision_value) > 0)
        OR
        (
            revision_kind = 'INTEGER'
            AND printf('%d', CAST(revision_value AS INTEGER)) = revision_value
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_quant_instrument_identity_lookup
    ON quant_instrument_identity(
        instrument_id, market, effective_from, known_at, usable_from
    );
CREATE INDEX IF NOT EXISTS idx_quant_instrument_identity_symbol
    ON quant_instrument_identity(symbol, market, effective_from);

CREATE TABLE IF NOT EXISTS quant_security_status (
    status_id          TEXT PRIMARY KEY CHECK (length(status_id) = 64),
    instrument_id      TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    market             TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    session_date       TEXT NOT NULL,
    listing_state      TEXT NOT NULL CHECK (
        listing_state IN ('PRE_LISTING', 'LISTED', 'DELISTING', 'DELISTED')
    ),
    trading_state      TEXT NOT NULL CHECK (
        trading_state IN ('TRADABLE', 'SUSPENDED', 'HALTED', 'UNKNOWN')
    ),
    risk_designation   TEXT NOT NULL CHECK (
        risk_designation IN ('NORMAL', 'ST', 'STAR_ST', 'RISK_WARNING', 'OTHER', 'UNKNOWN')
    ),
    known_at           TEXT NOT NULL,
    usable_from        TEXT NOT NULL,
    source             TEXT NOT NULL,
    revision_kind      TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value     TEXT NOT NULL,
    verified           INTEGER NOT NULL CHECK (verified IN (0, 1)),
    source_note        TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    CHECK (length(instrument_id) > 0),
    CHECK (length(symbol) > 0),
    CHECK (
        listing_state NOT IN ('PRE_LISTING', 'DELISTED')
        OR trading_state != 'TRADABLE'
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
CREATE INDEX IF NOT EXISTS idx_quant_security_status_lookup
    ON quant_security_status(
        instrument_id, market, session_date, known_at, usable_from
    );
CREATE INDEX IF NOT EXISTS idx_quant_security_status_symbol
    ON quant_security_status(symbol, market, session_date);

CREATE TABLE IF NOT EXISTS quant_universe_membership (
    membership_id      TEXT PRIMARY KEY CHECK (length(membership_id) = 64),
    universe_id        TEXT NOT NULL,
    instrument_id      TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    market             TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    effective_date     TEXT NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('INCLUDED', 'EXCLUDED')),
    known_at           TEXT NOT NULL,
    usable_from        TEXT NOT NULL,
    source             TEXT NOT NULL,
    universe_version   TEXT NOT NULL,
    revision_kind      TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value     TEXT NOT NULL,
    verified           INTEGER NOT NULL CHECK (verified IN (0, 1)),
    reason             TEXT NOT NULL,
    source_note        TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    CHECK (length(universe_id) > 0),
    CHECK (length(instrument_id) > 0),
    CHECK (length(symbol) > 0),
    CHECK (length(reason) > 0),
    CHECK (
        (revision_kind = 'STRING' AND length(revision_value) > 0)
        OR
        (
            revision_kind = 'INTEGER'
            AND printf('%d', CAST(revision_value AS INTEGER)) = revision_value
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_quant_universe_membership_lookup
    ON quant_universe_membership(
        universe_id, market, instrument_id, effective_date, known_at, usable_from
    );
CREATE INDEX IF NOT EXISTS idx_quant_universe_membership_symbol
    ON quant_universe_membership(universe_id, market, symbol, effective_date);

CREATE TRIGGER IF NOT EXISTS quant_universe_coverage_no_update
BEFORE UPDATE ON quant_universe_coverage
BEGIN
    SELECT RAISE(ABORT, 'quant_universe_coverage is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_universe_coverage_no_delete
BEFORE DELETE ON quant_universe_coverage
BEGIN
    SELECT RAISE(ABORT, 'quant_universe_coverage is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_instrument_identity_no_update
BEFORE UPDATE ON quant_instrument_identity
BEGIN
    SELECT RAISE(ABORT, 'quant_instrument_identity is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_instrument_identity_no_delete
BEFORE DELETE ON quant_instrument_identity
BEGIN
    SELECT RAISE(ABORT, 'quant_instrument_identity is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_security_status_no_update
BEFORE UPDATE ON quant_security_status
BEGIN
    SELECT RAISE(ABORT, 'quant_security_status is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_security_status_no_delete
BEFORE DELETE ON quant_security_status
BEGIN
    SELECT RAISE(ABORT, 'quant_security_status is append-only');
END;

CREATE TRIGGER IF NOT EXISTS quant_universe_membership_no_update
BEFORE UPDATE ON quant_universe_membership
BEGIN
    SELECT RAISE(ABORT, 'quant_universe_membership is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_universe_membership_no_delete
BEFORE DELETE ON quant_universe_membership
BEGIN
    SELECT RAISE(ABORT, 'quant_universe_membership is append-only');
END;
