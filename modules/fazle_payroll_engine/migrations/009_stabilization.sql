-- ============================================================================
-- Migration 009: Operational Stabilization Layer
-- ============================================================================
-- PHILOSOPHY: Additive only. No existing columns touched.
-- All statements guarded with IF NOT EXISTS / DO NOTHING.
-- Auto-runs via start_fpe() on next service restart.
-- ============================================================================

-- ── 1. Draft TTL — add expires_at to payment drafts ──────────────────────────
ALTER TABLE fazle_payment_drafts
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- Backfill: give existing pending drafts a 24-hour TTL from creation
UPDATE fazle_payment_drafts
SET    expires_at = created_at + INTERVAL '24 hours'
WHERE  expires_at IS NULL
  AND  status = 'pending';

-- Also add 'expired' to the set of terminal states (no constraint change needed,
-- just documenting the new status value here).

-- ── 2. Historical import flag on escort programs ──────────────────────────────
-- When TRUE: record was imported from history, NOT a live operational order.
-- Historical rows must never trigger approval drafts or admin notifications.
ALTER TABLE wbom_escort_programs
    ADD COLUMN IF NOT EXISTS is_historical BOOLEAN NOT NULL DEFAULT FALSE;

-- ── 3. Bridge heartbeat table ─────────────────────────────────────────────────
-- Each bridge/meta source writes here every time it successfully delivers a
-- message.  The watchdog scheduler job reads this to detect stale bridges.
CREATE TABLE IF NOT EXISTS fazle_bridge_heartbeats (
    bridge_id    TEXT        PRIMARY KEY,
    bridge_label TEXT        NOT NULL DEFAULT '',
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_msg_id  TEXT,
    status       TEXT        NOT NULL DEFAULT 'unknown',
    extra        JSONB
);

-- ── 4. Central message ingestion queue ────────────────────────────────────────
-- All inbound messages (WhatsApp, Meta, bridge) should be enqueued here before
-- routing/processing.  Provides dedup, ordering, retry, and observability.
CREATE TABLE IF NOT EXISTS fazle_message_queue (
    id             BIGSERIAL   PRIMARY KEY,
    enqueued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source         TEXT        NOT NULL,   -- bridge1, bridge2, meta, manual
    sender_phone   TEXT        NOT NULL,
    direction      TEXT        NOT NULL DEFAULT 'inbound',
    message_type   TEXT        NOT NULL DEFAULT 'text', -- text, image, audio, doc
    content_text   TEXT,
    media_url      TEXT,
    media_id       TEXT,
    idempotency_key TEXT       UNIQUE,     -- prevents double-enqueue
    status         TEXT        NOT NULL DEFAULT 'pending',  -- pending/processing/done/failed
    attempts       INT         NOT NULL DEFAULT 0,
    last_error     TEXT,
    processed_at   TIMESTAMPTZ,
    processor_id   TEXT,
    extra          JSONB
);

CREATE INDEX IF NOT EXISTS idx_msg_queue_status_enqueued
    ON fazle_message_queue(status, enqueued_at)
    WHERE status IN ('pending','failed');

CREATE INDEX IF NOT EXISTS idx_msg_queue_sender
    ON fazle_message_queue(sender_phone, enqueued_at DESC);

-- ── 5. Seed known bridges into heartbeat table ────────────────────────────────
INSERT INTO fazle_bridge_heartbeats (bridge_id, bridge_label, last_seen_at, status)
VALUES
    ('bridge1', 'HR-01958122300',  NOW() - INTERVAL '1 year', 'unknown'),
    ('bridge2', 'OPS-01880446111', NOW() - INTERVAL '1 year', 'unknown'),
    ('meta',    'Meta-WA-Business', NOW() - INTERVAL '1 year', 'unknown')
ON CONFLICT (bridge_id) DO NOTHING;
