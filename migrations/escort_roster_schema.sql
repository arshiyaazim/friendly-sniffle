-- ============================================================
-- Escort Roster Module — DB Extension Tables
-- Run ONCE against ai-postgres (database: postgres)
-- This is purely ADDITIVE — zero modifications to existing tables
-- ============================================================
-- SAFE: all new tables, new indexes, no alterations to wbom_*
-- ============================================================

BEGIN;

-- ── 1. Conveyance / Rate Config ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escort_calculation_config (
    id                      SERIAL PRIMARY KEY,
    destination             VARCHAR(120) NOT NULL,
    destination_key         VARCHAR(120) NOT NULL UNIQUE,   -- lowercase, stripped
    conveyance_amount       NUMERIC(10,2) NOT NULL DEFAULT 0,
    shift_rate              NUMERIC(10,2) NOT NULL DEFAULT 200,
    notes                   TEXT,
    updated_by              TEXT,
    updated_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed default conveyance values
INSERT INTO escort_calculation_config
    (destination, destination_key, conveyance_amount, shift_rate)
VALUES
    ('Narayanganj', 'narayanganj', 600,  200),
    ('Noapara',     'noapara',     1000, 200),
    ('Nagarbari',   'nagarbari',   900,  200),
    ('Aricha',      'aricha',      900,  200),
    ('Bhairab',     'bhairab',     800,  200),
    ('Ashuganj',    'ashuganj',    800,  200),
    ('Chandpur',    'chandpur',    700,  200),
    ('Barishal',    'barishal',    1200, 200),
    ('Mongla',      'mongla',      1500, 200)
ON CONFLICT (destination_key) DO NOTHING;


-- ── 2. Roster Entries ────────────────────────────────────────────────────────
-- One row per wbom_escort_programs row; stores calculated fields
CREATE TABLE IF NOT EXISTS escort_roster_entries (
    id                      SERIAL PRIMARY KEY,
    program_id              INTEGER NOT NULL UNIQUE,  -- points to wbom_escort_programs.program_id

    -- Mirrors / cache of key program fields (for fast roster queries without joins)
    mother_vessel           TEXT,
    lighter_vessel          TEXT,
    master_mobile           TEXT,
    escort_name             TEXT,
    escort_mobile           TEXT,
    destination             TEXT,
    start_date              DATE,
    start_shift             CHAR(1) CHECK (start_shift IN ('D','N')),
    end_date                DATE,
    end_shift               CHAR(1) CHECK (end_shift IN ('D','N')),

    -- Calculated pay fields
    total_shifts            INTEGER,
    total_days              NUMERIC(6,2),
    salary                  NUMERIC(10,2),
    conveyance              NUMERIC(10,2),
    total                   NUMERIC(10,2),
    release_point           TEXT,

    -- Lifecycle
    roster_status           VARCHAR(30) NOT NULL DEFAULT 'draft',
    -- draft | active | completed | cancelled | paid

    -- Metadata
    calc_version            INTEGER NOT NULL DEFAULT 1,
    notes                   TEXT,
    last_synced_at          TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ere_program_id    ON escort_roster_entries(program_id);
CREATE INDEX IF NOT EXISTS idx_ere_status         ON escort_roster_entries(roster_status);
CREATE INDEX IF NOT EXISTS idx_ere_start_date     ON escort_roster_entries(start_date);
CREATE INDEX IF NOT EXISTS idx_ere_mother_vessel  ON escort_roster_entries USING gin(to_tsvector('simple', COALESCE(mother_vessel,'')));
CREATE INDEX IF NOT EXISTS idx_ere_lighter_vessel ON escort_roster_entries USING gin(to_tsvector('simple', COALESCE(lighter_vessel,'')));


-- ── 3. Shift Logs ────────────────────────────────────────────────────────────
-- Granular per-shift activity events (optional, for timeline view)
CREATE TABLE IF NOT EXISTS escort_shift_logs (
    id                      SERIAL PRIMARY KEY,
    program_id              INTEGER NOT NULL,
    shift_date              DATE NOT NULL,
    shift                   CHAR(1) NOT NULL CHECK (shift IN ('D','N')),
    log_type                VARCHAR(30) NOT NULL,  -- start | ongoing | release | note
    notes                   TEXT,
    recorded_by             TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_esl_program_id ON escort_shift_logs(program_id);
CREATE INDEX IF NOT EXISTS idx_esl_date       ON escort_shift_logs(shift_date);


-- ── 4. Release / Slip Match Candidates ───────────────────────────────────────
-- Tracks OCR slip extraction → program match suggestions
CREATE TABLE IF NOT EXISTS escort_release_matches (
    id                      SERIAL PRIMARY KEY,
    extraction_id           INTEGER,           -- escort_slip_extractions.id
    program_id              INTEGER,           -- wbom_escort_programs.program_id
    match_confidence        NUMERIC(5,4) DEFAULT 0,
    match_reason            TEXT,
    matched_fields          JSONB DEFAULT '{}',
    admin_action            VARCHAR(20),       -- NULL=pending | keep | reject | edit
    admin_phone             TEXT,
    acted_at                TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_erm_extraction_id  ON escort_release_matches(extraction_id);
CREATE INDEX IF NOT EXISTS idx_erm_program_id     ON escort_release_matches(program_id);
CREATE INDEX IF NOT EXISTS idx_erm_admin_action   ON escort_release_matches(admin_action) WHERE admin_action IS NULL;


-- ── 5. Audit Log ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escort_roster_audit_logs (
    id                      SERIAL PRIMARY KEY,
    program_id              INTEGER NOT NULL,
    roster_entry_id         INTEGER,
    action                  VARCHAR(60) NOT NULL,  -- sync | recalculate | edit | status_change | delete
    old_data                JSONB,
    new_data                JSONB,
    performed_by            TEXT,
    source                  TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eral_program_id ON escort_roster_audit_logs(program_id);
CREATE INDEX IF NOT EXISTS idx_eral_action     ON escort_roster_audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_eral_created_at ON escort_roster_audit_logs(created_at DESC);


COMMIT;

-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT table_name, pg_size_pretty(pg_total_relation_size(table_name::regclass)) AS size
FROM (VALUES
    ('escort_calculation_config'),
    ('escort_roster_entries'),
    ('escort_shift_logs'),
    ('escort_release_matches'),
    ('escort_roster_audit_logs')
) t(table_name);
