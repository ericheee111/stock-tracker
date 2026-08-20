-- Stage 2B: append-only corporate-action coverage and normalized action plans.
-- Decimal terms are stored as canonical TEXT. Python contracts perform exact
-- Decimal validation and deterministic 50-digit adjustment calculations.

CREATE TABLE IF NOT EXISTS quant_corporate_action_coverage (
    coverage_id              TEXT PRIMARY KEY CHECK (
        length(coverage_id) = 64
        AND coverage_id NOT GLOB '*[^0-9a-f]*'
    ),
    instrument_id            TEXT NOT NULL,
    market                   TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    start_date               TEXT NOT NULL,
    end_date                 TEXT NOT NULL,
    source                   TEXT NOT NULL,
    action_version           TEXT NOT NULL,
    known_at                 TEXT NOT NULL,
    usable_from              TEXT NOT NULL,
    revision_kind            TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value           TEXT NOT NULL,
    supersedes_revision_kind TEXT CHECK (
        supersedes_revision_kind IS NULL
        OR supersedes_revision_kind IN ('INTEGER', 'STRING')
    ),
    supersedes_revision_value TEXT,
    verified                 INTEGER NOT NULL CHECK (verified IN (0, 1)),
    complete                 INTEGER NOT NULL CHECK (complete IN (0, 1)),
    source_note              TEXT NOT NULL,
    payload_json             TEXT NOT NULL,
    CHECK (length(instrument_id) > 0),
    CHECK (length(source) > 0),
    CHECK (length(action_version) > 0),
    CHECK (date(start_date) = start_date),
    CHECK (date(end_date) = end_date),
    CHECK (end_date >= start_date),
    CHECK (julianday(known_at) IS NOT NULL),
    CHECK (julianday(usable_from) IS NOT NULL),
    CHECK (julianday(usable_from) >= julianday(known_at)),
    CHECK ((verified = 0 AND complete = 0) OR length(source_note) > 0),
    CHECK (
        (revision_kind = 'STRING' AND length(revision_value) > 0)
        OR
        (
            revision_kind = 'INTEGER'
            AND printf('%d', CAST(revision_value AS INTEGER)) = revision_value
        )
    ),
    CHECK (
        (
            supersedes_revision_kind IS NULL
            AND supersedes_revision_value IS NULL
        )
        OR
        (
            supersedes_revision_kind = 'STRING'
            AND supersedes_revision_value IS NOT NULL
            AND length(supersedes_revision_value) > 0
        )
        OR
        (
            supersedes_revision_kind = 'INTEGER'
            AND supersedes_revision_value IS NOT NULL
            AND printf('%d', CAST(supersedes_revision_value AS INTEGER))
                = supersedes_revision_value
        )
    ),
    CHECK (
        supersedes_revision_kind IS NULL
        OR supersedes_revision_kind <> revision_kind
        OR supersedes_revision_value <> revision_value
    )
);
CREATE INDEX IF NOT EXISTS idx_quant_corporate_action_coverage_lookup
    ON quant_corporate_action_coverage(
        instrument_id, market, start_date, end_date, known_at, usable_from
    );

CREATE TABLE IF NOT EXISTS quant_corporate_action_fact (
    fact_id                   TEXT PRIMARY KEY CHECK (
        length(fact_id) = 64
        AND fact_id NOT GLOB '*[^0-9a-f]*'
    ),
    action_id                 TEXT NOT NULL,
    instrument_id             TEXT NOT NULL,
    identity_fact_id          TEXT NOT NULL CHECK (
        length(identity_fact_id) = 64
        AND identity_fact_id NOT GLOB '*[^0-9a-f]*'
    ),
    symbol                    TEXT NOT NULL,
    market                    TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    ex_date                   TEXT NOT NULL,
    record_date               TEXT,
    payment_date              TEXT,
    share_listing_date        TEXT,
    lifecycle                 TEXT NOT NULL CHECK (
        lifecycle IN ('ANNOUNCED', 'EFFECTIVE', 'CANCELLED')
    ),
    automatic_share_ratio     TEXT,
    cash_dividend_per_share   TEXT,
    rights_entitlement_ratio  TEXT,
    rights_subscription_price TEXT,
    currency                  TEXT,
    reference_price           TEXT,
    reference_price_snapshot_id TEXT CHECK (
        reference_price_snapshot_id IS NULL
        OR (
            length(reference_price_snapshot_id) = 64
            AND reference_price_snapshot_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    known_at                  TEXT NOT NULL,
    usable_from               TEXT NOT NULL,
    source                    TEXT NOT NULL,
    action_version            TEXT NOT NULL,
    revision_kind             TEXT NOT NULL CHECK (revision_kind IN ('INTEGER', 'STRING')),
    revision_value            TEXT NOT NULL,
    supersedes_revision_kind  TEXT CHECK (
        supersedes_revision_kind IS NULL
        OR supersedes_revision_kind IN ('INTEGER', 'STRING')
    ),
    supersedes_revision_value TEXT,
    verified                  INTEGER NOT NULL CHECK (verified IN (0, 1)),
    source_note               TEXT NOT NULL,
    payload_json              TEXT NOT NULL,
    FOREIGN KEY (identity_fact_id)
        REFERENCES quant_instrument_identity(identity_id),
    CHECK (length(action_id) > 0),
    CHECK (length(instrument_id) > 0),
    CHECK (length(symbol) > 0),
    CHECK (length(source) > 0),
    CHECK (length(action_version) > 0),
    CHECK (date(ex_date) = ex_date),
    CHECK (record_date IS NULL OR date(record_date) = record_date),
    CHECK (payment_date IS NULL OR date(payment_date) = payment_date),
    CHECK (share_listing_date IS NULL OR date(share_listing_date) = share_listing_date),
    CHECK (payment_date IS NULL OR payment_date >= ex_date),
    CHECK (share_listing_date IS NULL OR share_listing_date >= ex_date),
    CHECK (julianday(known_at) IS NOT NULL),
    CHECK (julianday(usable_from) IS NOT NULL),
    CHECK (julianday(usable_from) >= julianday(known_at)),
    CHECK (verified = 0 OR length(source_note) > 0),
    CHECK (
        (
            lifecycle = 'CANCELLED'
            AND automatic_share_ratio IS NULL
            AND cash_dividend_per_share IS NULL
            AND rights_entitlement_ratio IS NULL
            AND rights_subscription_price IS NULL
            AND currency IS NULL
            AND reference_price IS NULL
            AND reference_price_snapshot_id IS NULL
        )
        OR
        (
            lifecycle IN ('ANNOUNCED', 'EFFECTIVE')
            AND automatic_share_ratio IS NOT NULL
            AND cash_dividend_per_share IS NOT NULL
            AND rights_entitlement_ratio IS NOT NULL
        )
    ),
    CHECK (
        automatic_share_ratio IS NULL
        OR (
            length(automatic_share_ratio) > 0
            AND automatic_share_ratio = trim(automatic_share_ratio)
            AND automatic_share_ratio NOT GLOB '*[^0-9.]*'
            AND length(automatic_share_ratio)
                - length(replace(automatic_share_ratio, '.', '')) <= 1
            AND automatic_share_ratio NOT LIKE '.%'
            AND automatic_share_ratio NOT LIKE '%.'
            AND automatic_share_ratio <> '0'
            AND (
                substr(automatic_share_ratio, 1, 1) <> '0'
                OR substr(automatic_share_ratio, 1, 2) = '0.'
            )
            AND (
                instr(automatic_share_ratio, '.') = 0
                OR substr(automatic_share_ratio, -1, 1) <> '0'
            )
        )
    ),
    CHECK (
        cash_dividend_per_share IS NULL
        OR cash_dividend_per_share = '0'
        OR (
            length(cash_dividend_per_share) > 0
            AND cash_dividend_per_share = trim(cash_dividend_per_share)
            AND cash_dividend_per_share NOT GLOB '*[^0-9.]*'
            AND length(cash_dividend_per_share)
                - length(replace(cash_dividend_per_share, '.', '')) <= 1
            AND cash_dividend_per_share NOT LIKE '.%'
            AND cash_dividend_per_share NOT LIKE '%.'
            AND (
                substr(cash_dividend_per_share, 1, 1) <> '0'
                OR substr(cash_dividend_per_share, 1, 2) = '0.'
            )
            AND (
                instr(cash_dividend_per_share, '.') = 0
                OR substr(cash_dividend_per_share, -1, 1) <> '0'
            )
        )
    ),
    CHECK (
        rights_entitlement_ratio IS NULL
        OR rights_entitlement_ratio = '0'
        OR (
            length(rights_entitlement_ratio) > 0
            AND rights_entitlement_ratio = trim(rights_entitlement_ratio)
            AND rights_entitlement_ratio NOT GLOB '*[^0-9.]*'
            AND length(rights_entitlement_ratio)
                - length(replace(rights_entitlement_ratio, '.', '')) <= 1
            AND rights_entitlement_ratio NOT LIKE '.%'
            AND rights_entitlement_ratio NOT LIKE '%.'
            AND (
                substr(rights_entitlement_ratio, 1, 1) <> '0'
                OR substr(rights_entitlement_ratio, 1, 2) = '0.'
            )
            AND (
                instr(rights_entitlement_ratio, '.') = 0
                OR substr(rights_entitlement_ratio, -1, 1) <> '0'
            )
        )
    ),
    CHECK (
        rights_subscription_price IS NULL
        OR (
            length(rights_subscription_price) > 0
            AND rights_subscription_price = trim(rights_subscription_price)
            AND rights_subscription_price NOT GLOB '*[^0-9.]*'
            AND length(rights_subscription_price)
                - length(replace(rights_subscription_price, '.', '')) <= 1
            AND rights_subscription_price NOT LIKE '.%'
            AND rights_subscription_price NOT LIKE '%.'
            AND rights_subscription_price <> '0'
            AND (
                substr(rights_subscription_price, 1, 1) <> '0'
                OR substr(rights_subscription_price, 1, 2) = '0.'
            )
            AND (
                instr(rights_subscription_price, '.') = 0
                OR substr(rights_subscription_price, -1, 1) <> '0'
            )
        )
    ),
    CHECK (
        reference_price IS NULL
        OR (
            length(reference_price) > 0
            AND reference_price = trim(reference_price)
            AND reference_price NOT GLOB '*[^0-9.]*'
            AND length(reference_price)
                - length(replace(reference_price, '.', '')) <= 1
            AND reference_price NOT LIKE '.%'
            AND reference_price NOT LIKE '%.'
            AND reference_price <> '0'
            AND (
                substr(reference_price, 1, 1) <> '0'
                OR substr(reference_price, 1, 2) = '0.'
            )
            AND (
                instr(reference_price, '.') = 0
                OR substr(reference_price, -1, 1) <> '0'
            )
        )
    ),
    CHECK (
        lifecycle = 'CANCELLED'
        OR automatic_share_ratio <> '1'
        OR cash_dividend_per_share <> '0'
        OR rights_entitlement_ratio <> '0'
    ),
    CHECK (
        lifecycle = 'CANCELLED'
        OR automatic_share_ratio = '1'
        OR share_listing_date IS NOT NULL
    ),
    CHECK (
        (rights_entitlement_ratio = '0' AND rights_subscription_price IS NULL)
        OR
        (rights_entitlement_ratio <> '0' AND rights_subscription_price IS NOT NULL)
    ),
    CHECK (
        currency IS NULL
        OR (
            length(currency) = 3
            AND currency = upper(currency)
            AND currency NOT GLOB '*[^A-Z]*'
        )
    ),
    CHECK (
        lifecycle = 'CANCELLED'
        OR (
            cash_dividend_per_share = '0'
            AND rights_entitlement_ratio = '0'
            AND reference_price IS NULL
            AND currency IS NULL
        )
        OR (
            (
                cash_dividend_per_share <> '0'
                OR rights_entitlement_ratio <> '0'
                OR reference_price IS NOT NULL
            )
            AND currency IS NOT NULL
        )
    ),
    CHECK (
        (
            reference_price IS NULL
            AND reference_price_snapshot_id IS NULL
        )
        OR
        (
            reference_price IS NOT NULL
            AND reference_price_snapshot_id IS NOT NULL
        )
    ),
    CHECK (
        (revision_kind = 'STRING' AND length(revision_value) > 0)
        OR
        (
            revision_kind = 'INTEGER'
            AND printf('%d', CAST(revision_value AS INTEGER)) = revision_value
        )
    ),
    CHECK (
        (
            supersedes_revision_kind IS NULL
            AND supersedes_revision_value IS NULL
        )
        OR
        (
            supersedes_revision_kind = 'STRING'
            AND supersedes_revision_value IS NOT NULL
            AND length(supersedes_revision_value) > 0
        )
        OR
        (
            supersedes_revision_kind = 'INTEGER'
            AND supersedes_revision_value IS NOT NULL
            AND printf('%d', CAST(supersedes_revision_value AS INTEGER))
                = supersedes_revision_value
        )
    ),
    CHECK (
        supersedes_revision_kind IS NULL
        OR supersedes_revision_kind <> revision_kind
        OR supersedes_revision_value <> revision_value
    )
);
CREATE INDEX IF NOT EXISTS idx_quant_corporate_action_fact_lookup
    ON quant_corporate_action_fact(
        instrument_id, market, ex_date, known_at, usable_from
    );
CREATE INDEX IF NOT EXISTS idx_quant_corporate_action_fact_action
    ON quant_corporate_action_fact(action_id, revision_kind, revision_value);

CREATE TRIGGER IF NOT EXISTS quant_corporate_action_fact_identity_guard
BEFORE INSERT ON quant_corporate_action_fact
WHEN NOT EXISTS (
    SELECT 1
    FROM quant_instrument_identity AS identity_fact
    WHERE identity_fact.identity_id = NEW.identity_fact_id
      AND identity_fact.instrument_id = NEW.instrument_id
      AND identity_fact.symbol = NEW.symbol
      AND identity_fact.market = NEW.market
      AND identity_fact.effective_from <= NEW.ex_date
      AND (
          identity_fact.effective_to IS NULL
          OR identity_fact.effective_to >= NEW.ex_date
      )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'corporate action identity must be active and match instrument/symbol/market'
    );
END;

CREATE TRIGGER IF NOT EXISTS quant_corporate_action_coverage_no_update
BEFORE UPDATE ON quant_corporate_action_coverage
BEGIN
    SELECT RAISE(ABORT, 'quant_corporate_action_coverage is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_corporate_action_coverage_no_delete
BEFORE DELETE ON quant_corporate_action_coverage
BEGIN
    SELECT RAISE(ABORT, 'quant_corporate_action_coverage is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_corporate_action_fact_no_update
BEFORE UPDATE ON quant_corporate_action_fact
BEGIN
    SELECT RAISE(ABORT, 'quant_corporate_action_fact is append-only');
END;
CREATE TRIGGER IF NOT EXISTS quant_corporate_action_fact_no_delete
BEFORE DELETE ON quant_corporate_action_fact
BEGIN
    SELECT RAISE(ABORT, 'quant_corporate_action_fact is append-only');
END;
