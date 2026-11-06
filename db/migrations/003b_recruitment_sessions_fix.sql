-- Migration 003b: Fix recruitment sessions table — correct column names
-- The prior migration (003) created fazle_recruitment_sessions with wrong columns.
-- Drop and recreate (table was just created — no real data yet).

BEGIN;

DROP TABLE IF EXISTS fazle_recruitment_sessions;

CREATE TABLE fazle_recruitment_sessions (
    id               bigserial PRIMARY KEY,
    phone            text NOT NULL,
    source_bridge    text NOT NULL DEFAULT 'bridge1',
    source_message   text,
    -- Collection step tracker
    collection_step  text NOT NULL DEFAULT 'name',  -- name|age|area|job_preference|experience|phone_confirm
    funnel_stage     text NOT NULL DEFAULT 'collecting', -- collecting|new|scored|abandoned
    -- Collected fields (filled in as conversation progresses)
    full_name        text,
    age              integer,
    area             text,
    job_preference   text,
    experience_years integer DEFAULT 0,
    confirmed_phone  text,
    -- Scoring (filled when complete)
    score            integer,
    score_bucket     text,  -- hot|warm|cold
    -- Link to canonical candidate table once scored
    candidate_id     integer REFERENCES wbom_candidates(candidate_id) ON DELETE SET NULL,
    -- Timestamps
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Only one active session per phone
CREATE UNIQUE INDEX idx_recruitment_sessions_phone_active
    ON fazle_recruitment_sessions (phone)
    WHERE funnel_stage IN ('collecting', 'new');

CREATE INDEX idx_recruitment_sessions_phone
    ON fazle_recruitment_sessions (phone);

CREATE INDEX idx_recruitment_sessions_stage
    ON fazle_recruitment_sessions (funnel_stage);

COMMIT;

SELECT 'fazle_recruitment_sessions created with correct schema' AS result;
