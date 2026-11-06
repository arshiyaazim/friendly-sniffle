-- Migration 012 — Phase 13B: Global Queue Arbitration Layer
-- Run: docker exec -i ai-postgres psql -U postgres -d postgres
-- Applied: 2026-05-14

-- ── 1. Lease table ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fazle_queue_leases (
    lease_id      BIGSERIAL    PRIMARY KEY,
    message_id    BIGINT       NOT NULL REFERENCES fazle_message_queue(id)
                                   ON DELETE CASCADE,
    intent        TEXT         NOT NULL DEFAULT 'generic',
    worker_id     TEXT         NOT NULL,
    status        TEXT         NOT NULL DEFAULT 'leased',
    attempts      INTEGER      NOT NULL DEFAULT 1,
    leased_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW() + INTERVAL '120 seconds',
    completed_at  TIMESTAMPTZ,
    retry_after   TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_error    TEXT,
    metadata_json JSONB        NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT chk_lease_status CHECK (
        status IN ('leased','processing','completed','failed','dead_letter')
    )
);

-- ── 2. Unique constraint: one active lease per (message_id, intent) pair ───
-- ON CONFLICT (message_id, intent) used in acquire_lease INSERT
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_leases_msg_intent
    ON fazle_queue_leases (message_id, intent);

-- ── 3. Operational indexes ─────────────────────────────────────────────────
-- Stale lease sweep
CREATE INDEX IF NOT EXISTS idx_queue_leases_expires
    ON fazle_queue_leases (expires_at)
    WHERE status IN ('leased','processing');

-- Dead-letter inspection
CREATE INDEX IF NOT EXISTS idx_queue_leases_dead
    ON fazle_queue_leases (updated_at DESC)
    WHERE status = 'dead_letter';

-- Worker-level query ("what am I holding?")
CREATE INDEX IF NOT EXISTS idx_queue_leases_worker
    ON fazle_queue_leases (worker_id, status);

-- ── 4. Add retry_after index on fazle_message_queue (additive) ────────────
-- Allows efficient "eligible pending items" queries with backoff
CREATE INDEX IF NOT EXISTS idx_msg_queue_retry
    ON fazle_message_queue (status, attempts, enqueued_at)
    WHERE status = 'pending';

-- Verify
SELECT
    table_name,
    pg_size_pretty(pg_total_relation_size(quote_ident(table_name))) AS size
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('fazle_queue_leases', 'fazle_message_queue')
ORDER BY table_name;
