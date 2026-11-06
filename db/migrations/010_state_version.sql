-- Migration 010 — Phase 12G: Global state version table (PostgreSQL fallback)
-- Redis is the primary counter (INCR fazle:state_version).
-- This table is only written to when Redis is unavailable.
-- Safe to run multiple times (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS fazle_state_version (
    id         SERIAL       PRIMARY KEY,
    version    BIGINT       NOT NULL,
    bumped_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Ensure fast MAX(version) lookups
CREATE INDEX IF NOT EXISTS idx_state_version_bumped_at
    ON fazle_state_version (bumped_at DESC);

-- Seed row so MAX(version) never returns NULL
INSERT INTO fazle_state_version (version, bumped_at)
SELECT 0, NOW()
WHERE NOT EXISTS (SELECT 1 FROM fazle_state_version);
