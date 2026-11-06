-- Migration 007: Reviewed Reply Memory (Batch 26)
-- Adds a reusable, operator-reviewed reply store for conservative
-- same-intent reply reuse.

CREATE TABLE IF NOT EXISTS fazle_reviewed_replies (
    id                      BIGSERIAL PRIMARY KEY,
    source_draft_id         BIGINT NOT NULL,
    source                  TEXT NOT NULL,
    intent                  TEXT NOT NULL,
    draft_type              TEXT NOT NULL DEFAULT 'generic',
    role                    TEXT,
    recipient_phone         TEXT,
    last10_phone            TEXT,
    language                TEXT,
    normalized_trigger_text TEXT,
    match_scope             TEXT NOT NULL DEFAULT 'intent_role_phone',
    reply_text              TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'active',
    priority                INTEGER NOT NULL DEFAULT 100,
    usage_count             INTEGER NOT NULL DEFAULT 0,
    last_used_at            TIMESTAMPTZ,
    created_by              TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    meta                    JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_reviewed_replies_scope
    ON fazle_reviewed_replies (intent, role, draft_type, status);

CREATE INDEX IF NOT EXISTS idx_reviewed_replies_phone
    ON fazle_reviewed_replies (last10_phone, status);

CREATE INDEX IF NOT EXISTS idx_reviewed_replies_source_draft
    ON fazle_reviewed_replies (source_draft_id);