-- Migration 011 — Phase 13A: Distributed Runtime Gateway
--
-- Stores one row per running app-process (fazle-core, payroll-engine,
-- escort-roster, standalone scripts) so each node can discover peers,
-- detect crashes, and redistribute work.
--
-- Safe to run multiple times (IF NOT EXISTS / IF NOT EXISTS guards).

CREATE TABLE IF NOT EXISTS fazle_runtime_nodes (
    node_id         TEXT        PRIMARY KEY,
    app_name        TEXT        NOT NULL,
    role            TEXT        NOT NULL DEFAULT 'worker',
    status          TEXT        NOT NULL DEFAULT 'online',   -- online | offline | degraded
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version         TEXT,
    active_requests INTEGER     NOT NULL DEFAULT 0,
    queue_depth     INTEGER     NOT NULL DEFAULT 0,
    metadata_json   JSONB       NOT NULL DEFAULT '{}'::jsonb
);

-- Fast lookups by status (health dashboard, stale sweep)
CREATE INDEX IF NOT EXISTS idx_runtime_nodes_status
    ON fazle_runtime_nodes (status);

-- Filter by app_name across multi-instance deploys
CREATE INDEX IF NOT EXISTS idx_runtime_nodes_app_name
    ON fazle_runtime_nodes (app_name);

-- Stale-sweep: order by least-recently-seen
CREATE INDEX IF NOT EXISTS idx_runtime_nodes_last_seen
    ON fazle_runtime_nodes (last_seen DESC);

-- Constraint: status values are bounded
ALTER TABLE fazle_runtime_nodes
    DROP CONSTRAINT IF EXISTS chk_runtime_nodes_status;
ALTER TABLE fazle_runtime_nodes
    ADD CONSTRAINT chk_runtime_nodes_status
    CHECK (status IN ('online', 'offline', 'degraded'));
