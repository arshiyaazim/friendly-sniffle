-- Migration 005: Unified Admin Approval Workflow
-- Adds draft_type + meta to fazle_draft_replies for attendance/escort/payment routing
-- Adds source (bridge name) to fazle_payment_drafts for employee reply routing

ALTER TABLE fazle_draft_replies
    ADD COLUMN IF NOT EXISTS draft_type TEXT DEFAULT 'generic',
    ADD COLUMN IF NOT EXISTS meta      JSONB DEFAULT '{}';

ALTER TABLE fazle_payment_drafts
    ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'bridge1';

CREATE INDEX IF NOT EXISTS idx_draft_replies_type
    ON fazle_draft_replies (draft_type, status);
