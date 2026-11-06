-- =============================================================================
-- Escort Roster — History & Order Group Schema
-- Migration: escort_history_schema.sql
-- Run: psql -U postgres -d postgres -f escort_history_schema.sql
-- =============================================================================

-- Parent order table: one row per WhatsApp message containing escort order(s)
CREATE TABLE IF NOT EXISTS escort_order_groups (
    group_id        SERIAL PRIMARY KEY,
    source          VARCHAR(30) NOT NULL DEFAULT 'conversation_file',
                    -- 'conversation_file' | 'realtime' | 'manual'
    sender_phone    VARCHAR(20),
    direction       VARCHAR(10) NOT NULL DEFAULT 'outbound',
                    -- 'outbound' (admin→client) | 'inbound' (client→admin)
    message_ts      TIMESTAMP WITH TIME ZONE,
    raw_text        TEXT NOT NULL DEFAULT '',
    mother_vessel   VARCHAR(200),
    destination     VARCHAR(100),
    lighter_count   INTEGER NOT NULL DEFAULT 0,
    processed       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eog_message_ts     ON escort_order_groups(message_ts);
CREATE INDEX IF NOT EXISTS idx_eog_sender_phone   ON escort_order_groups(sender_phone);
CREATE INDEX IF NOT EXISTS idx_eog_mother_vessel  ON escort_order_groups(mother_vessel);

-- Child lighter table: one row per lighter/escort assignment within a group
CREATE TABLE IF NOT EXISTS escort_order_lighters (
    lighter_id          SERIAL PRIMARY KEY,
    group_id            INTEGER NOT NULL,
                        -- FK to escort_order_groups (no hard FK to keep it safe)
    program_id          INTEGER,
                        -- FK to wbom_escort_programs when matched
    mother_vessel       VARCHAR(200),
    lighter_vessel      VARCHAR(200),
    master_mobile       VARCHAR(20),
    escort_name         VARCHAR(100),
    escort_mobile       VARCHAR(20),
    start_date          DATE,
    start_shift         VARCHAR(1),
    destination         VARCHAR(100),
    match_confidence    FLOAT NOT NULL DEFAULT 0,
    match_method        VARCHAR(30),
                        -- 'exact_mobile' | 'fuzzy_vessel' | 'date_name' | NULL
    status              VARCHAR(20) NOT NULL DEFAULT 'unmatched',
                        -- 'unmatched' | 'matched' | 'linked'
    raw_block           TEXT,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eol_group_id       ON escort_order_lighters(group_id);
CREATE INDEX IF NOT EXISTS idx_eol_program_id     ON escort_order_lighters(program_id);
CREATE INDEX IF NOT EXISTS idx_eol_escort_mobile  ON escort_order_lighters(escort_mobile);
CREATE INDEX IF NOT EXISTS idx_eol_lighter_vessel ON escort_order_lighters(lighter_vessel);

-- Add draft expiry to escort_roster_entries for 48h auto-cleanup
ALTER TABLE escort_roster_entries
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE;

-- Set expiry for existing drafts: 48h from now (gives them a grace period)
UPDATE escort_roster_entries
SET expires_at = NOW() + INTERVAL '48 hours'
WHERE roster_status = 'draft' AND expires_at IS NULL;
