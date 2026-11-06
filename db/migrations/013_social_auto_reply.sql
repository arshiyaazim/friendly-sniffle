-- Social auto-reply backend tables.
-- Non-destructive: only creates new tables/indexes when missing.

CREATE TABLE IF NOT EXISTS social_inbox_events (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sender_id TEXT,
    sender_name TEXT,
    conversation_id TEXT,
    message_id TEXT,
    comment_id TEXT,
    parent_id TEXT,
    message_text TEXT,
    media_flag BOOLEAN NOT NULL DEFAULT FALSE,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    reply_status TEXT NOT NULL DEFAULT 'pending',
    classification TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_social_inbox_platform_received
    ON social_inbox_events(platform, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_social_inbox_reply_status
    ON social_inbox_events(reply_status);

CREATE TABLE IF NOT EXISTS social_reply_queue (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT REFERENCES social_inbox_events(id) ON DELETE SET NULL,
    platform TEXT NOT NULL,
    target_id TEXT NOT NULL,
    conversation_id TEXT,
    reply_to_comment_id TEXT,
    source_bridge TEXT,
    reply_text TEXT NOT NULL,
    intent TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_social_reply_queue_status_due
    ON social_reply_queue(status, next_retry_at);

CREATE TABLE IF NOT EXISTS social_sent_log (
    id BIGSERIAL PRIMARY KEY,
    queue_id BIGINT,
    event_id BIGINT,
    platform TEXT NOT NULL,
    target_id TEXT NOT NULL,
    external_id TEXT,
    reply_text TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS social_retry_queue (
    id BIGSERIAL PRIMARY KEY,
    queue_id BIGINT NOT NULL UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS social_flagged_items (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT REFERENCES social_inbox_events(id) ON DELETE SET NULL,
    platform TEXT NOT NULL,
    target_id TEXT,
    reason TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'manual_review',
    message_text TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_social_flagged_status
    ON social_flagged_items(status, created_at DESC);

CREATE TABLE IF NOT EXISTS social_backlog_state (
    state_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    last_cursor TEXT,
    last_checked_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS social_rate_limit_state (
    channel TEXT PRIMARY KEY,
    next_allowed_at TIMESTAMPTZ,
    last_sent_at TIMESTAMPTZ,
    sent_count_window INTEGER NOT NULL DEFAULT 0,
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS social_thread_state (
    id BIGSERIAL PRIMARY KEY,
    platform TEXT NOT NULL,
    target_id TEXT NOT NULL,
    answered_intents TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    last_reply_text TEXT,
    context_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(platform, target_id)
);