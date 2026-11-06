-- Fazle Core — Phase 5 DB Merge Preview
-- READ-ONLY inspection queries. Nothing is dropped.
-- Run these to understand the data before any migration.
--
-- All real data lives in the `postgres` database.
-- The `waerp` database has only 12 reply_templates and 3 escort_clients rows.

-- ── 1. Count rows in key tables ───────────────────────────────────────────────
SELECT 'wbom_employees'        AS tbl, COUNT(*) AS rows FROM wbom_employees
UNION ALL
SELECT 'wbom_contacts',                COUNT(*)         FROM wbom_contacts
UNION ALL
SELECT 'wbom_whatsapp_messages',       COUNT(*)         FROM wbom_whatsapp_messages
UNION ALL
SELECT 'wbom_escort_programs',         COUNT(*)         FROM wbom_escort_programs
UNION ALL
SELECT 'wbom_cash_transactions',       COUNT(*)         FROM wbom_cash_transactions
UNION ALL
SELECT 'wbom_attendance',              COUNT(*)         FROM wbom_attendance
UNION ALL
SELECT 'wbom_candidates',              COUNT(*)         FROM wbom_candidates
UNION ALL
SELECT 'fazle_leads',                  COUNT(*)         FROM fazle_leads;

-- ── 2. Sample contacts (latest 5) ─────────────────────────────────────────────
SELECT contact_id, whatsapp_number, display_name, relation_type_id, last_seen
FROM wbom_contacts
ORDER BY last_seen DESC NULLS LAST
LIMIT 5;

-- ── 3. Sample employees ────────────────────────────────────────────────────────
SELECT employee_id, employee_name, employee_mobile, designation, status
FROM wbom_employees
ORDER BY employee_id DESC
LIMIT 5;

-- ── 4. Recent messages ────────────────────────────────────────────────────────
SELECT id, whatsapp_number, LEFT(message_content, 60) AS preview,
       direction, platform, created_at
FROM wbom_whatsapp_messages
ORDER BY created_at DESC NULLS LAST
LIMIT 10;

-- ── 5. Active escort programs ─────────────────────────────────────────────────
SELECT program_id, program_date, vessel_name,
       escort_employee_id, status
FROM wbom_escort_programs
ORDER BY program_date DESC NULLS LAST
LIMIT 10;

-- ── 6. Columns that fazle-core needs to write ─────────────────────────────────
-- wbom_whatsapp_messages columns:
\d wbom_whatsapp_messages

-- ── 7. Check for 'platform' column (needed for source tracking) ───────────────
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'wbom_whatsapp_messages'
  AND table_schema = 'public'
ORDER BY ordinal_position;

-- ── 8. If 'platform' column doesn't exist, add it: ────────────────────────────
-- ALTER TABLE wbom_whatsapp_messages ADD COLUMN IF NOT EXISTS platform TEXT DEFAULT 'unknown';
-- ALTER TABLE wbom_whatsapp_messages ADD COLUMN IF NOT EXISTS direction TEXT DEFAULT 'inbound';
