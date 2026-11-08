-- Migration 091: outbound-message provenance + first-connection history backfill
-- ==========================================================================
--
-- WHY THIS EXISTS
-- ---------------
-- `wbom_whatsapp_messages` was written by three different code paths with
-- three different ideas of what a row means (see the README section
-- "Outbound messages, bridges and AI provenance"):
--
--   1. inbound raw copy (`bridge_poller._save_raw_core_copy`) — full fidelity,
--      `canonical_phone` / `phone_last10` / `original_sender_number` filled,
--      `platform` = the bridge that received it.
--   2. the shared writer (`bridge_poller._save_message`) — used for outbound
--      rows and one inbound fallback. It never filled `canonical_phone`,
--      `phone_last10` or `original_sender_number`, and it recorded the bridge
--      name only inside `platform`.
--   3. the queue dispatcher (`modules.outbound.sweep_once`) — the ONLY code
--      that actually hands a message to a bridge (`BridgeClient.send_strict`).
--      It did not write `wbom_whatsapp_messages` at all: it only flipped
--      `fazle_outbound_queue.status='sent'` and stored `external_id`.
--
-- Consequences this migration + the matching code changes fix:
--   * an outbound row existed only when its call site happened to pre-save
--     one, and that row was written BEFORE the send: it could not say whether
--     the message was delivered, and it named the originally requested bridge
--     even when the queue failed over to `fallback_channel`.
--   * nothing in the schema said whether an outbound body was produced by an
--     LLM (ollama / groq / omniroute / kimi / a phase3_ai_routes route) or by
--     a deterministic rule/template, so "was this an AI reply?" was
--     unanswerable from the database.
--   * history already present in a bridge's WhatsApp store (whatsmeow
--     `from_history_sync` rows, plus every `is_from_me=1` row) was never
--     imported, so a freshly paired device left the main DB empty for every
--     conversation that happened before pairing.
--
-- DESIGN NOTES
-- ------------
-- * `ai_generated` is NULLABLE on purpose: NULL = "unknown / not recorded"
--   (every row that predates this migration, and history-imported outbound
--   rows whose origin genuinely cannot be proven), TRUE = an LLM produced the
--   body, FALSE = deterministic rule, template, human typing or unknown-but-
--   not-AI. Queries must use `ai_generated IS TRUE`, never `= false`, when
--   they mean "AI wrote this".
-- * `source_bridge` is the bridge that actually carried the message. For
--   outbound rows written by the queue dispatcher this is the bridge that
--   accepted the send (after failover, if any) — not the one originally
--   requested.
-- * `outbound_queue_id` is UNIQUE (partial index) so the send-success write is
--   an idempotent upsert keyed on the queue row, and a retried send can never
--   create a second message row.
-- * `wbom_bridge_history_backfill` is the resumable state of the one-time
--   import. One row per bridge; `status` moves pending -> running ->
--   completed/failed. Nothing in the live poller reads or writes it.

BEGIN;

-- ── 1. Message-level provenance columns ────────────────────────────────────
ALTER TABLE wbom_whatsapp_messages
    ADD COLUMN IF NOT EXISTS source_bridge     text,
    ADD COLUMN IF NOT EXISTS ai_generated      boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS ai_provider       text,
    ADD COLUMN IF NOT EXISTS ai_model          text,
    ADD COLUMN IF NOT EXISTS ai_task           text,
    ADD COLUMN IF NOT EXISTS outbound_queue_id bigint,
    ADD COLUMN IF NOT EXISTS delivery_status   text,
    ADD COLUMN IF NOT EXISTS external_id       text,
    ADD COLUMN IF NOT EXISTS history_imported  boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.wbom_whatsapp_messages.source_bridge IS
    'Bridge that actually carried this message (bridge1/bridge2/bridge3/meta/...). For outbound rows this is the accepting bridge after any failover.';
COMMENT ON COLUMN public.wbom_whatsapp_messages.ai_generated IS
    'TRUE = an LLM produced the body; FALSE = rule/template/human; NULL = unknown (pre-091 rows, history imports). Query with IS TRUE.';
COMMENT ON COLUMN public.wbom_whatsapp_messages.ai_provider IS
    'LLM provider that produced the body (ollama, groq, omniroute, kimi, github_models, phase3_ai_routes:<route_id>, ...).';
COMMENT ON COLUMN public.wbom_whatsapp_messages.ai_model IS
    'Model name reported by the provider that produced the body.';
COMMENT ON COLUMN public.wbom_whatsapp_messages.outbound_queue_id IS
    'fazle_outbound_queue.id this outbound row was delivered through (idempotency key for the send-success write).';
COMMENT ON COLUMN public.wbom_whatsapp_messages.delivery_status IS
    'outbound transport outcome: sent | failed | dlq | unknown (history import).';
COMMENT ON COLUMN public.wbom_whatsapp_messages.history_imported IS
    'TRUE = imported from a bridge store by modules.history_backfill (never routed, never auto-replied).';

CREATE INDEX IF NOT EXISTS idx_wbom_messages_source_bridge_time
    ON wbom_whatsapp_messages (source_bridge, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_wbom_messages_ai_generated_time
    ON wbom_whatsapp_messages (received_at DESC)
    WHERE ai_generated IS TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_wbom_messages_outbound_queue
    ON wbom_whatsapp_messages (outbound_queue_id)
    WHERE outbound_queue_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_wbom_messages_history_imported
    ON wbom_whatsapp_messages (source_bridge, received_at DESC)
    WHERE history_imported IS TRUE;

-- ── 2. Backfill existing rows ──────────────────────────────────────────────
-- 2a. source_bridge for rows that only ever recorded the bridge in `platform`.
UPDATE wbom_whatsapp_messages
   SET source_bridge = platform
 WHERE source_bridge IS NULL
   AND platform IS NOT NULL
   AND platform <> '';

-- 2b. canonical_phone / phone_last10 for outbound rows written by the shared
--     writer (which never normalised). Mirrors
--     modules.phone_normalizer.storage_canonical_phone:
--       * 01[3-9]XXXXXXXX  (11 digits) -> 8801[3-9]XXXXXXXX
--       * 8801[3-9]XXXXXXXX (13)       -> unchanged
--       * any other 10..15 digits      -> digits only (foreign numbers kept)
--       * anything else (LID sentinel, PSID, empty) -> left NULL
UPDATE wbom_whatsapp_messages
   SET canonical_phone = CASE
         WHEN length(regexp_replace(COALESCE(sender_number, ''), '[^0-9]', '', 'g')) = 11
              AND regexp_replace(COALESCE(sender_number, ''), '[^0-9]', '', 'g') ~ '^01[3-9][0-9]{8}$'
              THEN '88' || regexp_replace(sender_number, '[^0-9]', '', 'g')
         WHEN length(regexp_replace(COALESCE(sender_number, ''), '[^0-9]', '', 'g')) BETWEEN 10 AND 15
              THEN regexp_replace(sender_number, '[^0-9]', '', 'g')
         ELSE canonical_phone
       END
 WHERE direction = 'outbound'
   AND canonical_phone IS NULL
   AND sender_number IS NOT NULL
   AND regexp_replace(COALESCE(sender_number, ''), '[^0-9]', '', 'g') <> '';

UPDATE wbom_whatsapp_messages
   SET phone_last10 = right(canonical_phone, 10)
 WHERE direction = 'outbound'
   AND canonical_phone IS NOT NULL
   AND length(canonical_phone) >= 10
   AND phone_last10 IS NULL;

-- 2c. delivery_status for pre-existing outbound rows: they were persisted at
--     enqueue time and nothing ever confirmed the transport outcome. Saying
--     "unknown" is the only honest value.
UPDATE wbom_whatsapp_messages
   SET delivery_status = 'unknown'
 WHERE direction = 'outbound'
   AND delivery_status IS NULL;

-- 2d. ai_generated: every row that predates this migration is genuinely
--     unknown. ADD COLUMN gave them the `false` default one statement ago, so
--     re-label those rows NULL instead of pretending they are provably
--     non-AI. Safe as a one-shot: db/migrate.py applies each file exactly
--     once (schema_migrations), and every row written after this point sets
--     the column explicitly.
UPDATE wbom_whatsapp_messages
   SET ai_generated = NULL
 WHERE ai_generated IS FALSE;

-- ── 3. One-time history-backfill state ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS wbom_bridge_history_backfill (
    bridge              text PRIMARY KEY,
    status              text NOT NULL DEFAULT 'pending',
    since_ts            timestamptz,
    until_ts            timestamptz,
    include_outbound    boolean NOT NULL DEFAULT true,
    last_source_ts      timestamptz,
    last_rowid          bigint NOT NULL DEFAULT 0,
    inbound_imported    bigint NOT NULL DEFAULT 0,
    outbound_imported   bigint NOT NULL DEFAULT 0,
    skipped_duplicate   bigint NOT NULL DEFAULT 0,
    skipped_unsupported bigint NOT NULL DEFAULT 0,
    contacts_upserted   bigint NOT NULL DEFAULT 0,
    identity_resolved   bigint NOT NULL DEFAULT 0,
    error               text,
    started_at          timestamptz,
    finished_at         timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT wbom_bridge_history_backfill_status_check
        CHECK (status IN ('pending', 'running', 'completed', 'failed'))
);

COMMENT ON TABLE public.wbom_bridge_history_backfill IS
    'Resumable state of the one-time bridge-store history import (modules.history_backfill). One row per bridge.';

COMMIT;
