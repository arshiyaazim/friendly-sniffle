-- Migration 001: Safe Mode draft replies + Escort Slip Extractions
-- Run once: psql $DATABASE_URL -f this_file.sql

-- ── Draft replies (Safe Mode suppressed outbound messages) ─────────────────────
CREATE TABLE IF NOT EXISTS fazle_draft_replies (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,          -- meta | bridge1 | bridge2
    recipient   TEXT NOT NULL,          -- phone number
    reply_text  TEXT NOT NULL,
    intent      TEXT,
    draft_only  BOOLEAN DEFAULT true,
    reviewed    BOOLEAN DEFAULT false,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_draft_replies_created
    ON fazle_draft_replies (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_draft_replies_recipient
    ON fazle_draft_replies (recipient);

-- ── Escort Slip Extractions ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escort_slip_extractions (
    id              BIGSERIAL PRIMARY KEY,
    source_file     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    document_type   TEXT,               -- printed_template_slip | handwritten_blank_slip | mixed_form | unknown_document
    mother_vessel   TEXT,
    lighter_vessel  TEXT,
    master_mobile   TEXT,
    escort_name     TEXT,
    escort_mobile   TEXT,
    start_date      TEXT,
    completion_date TEXT,
    release_place   TEXT,
    signatures_json JSONB,
    confidence      NUMERIC(4,2),
    raw_text        TEXT
);

CREATE INDEX IF NOT EXISTS idx_escort_slip_created
    ON escort_slip_extractions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_escort_slip_vessel
    ON escort_slip_extractions (mother_vessel);
