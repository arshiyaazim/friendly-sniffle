-- Migration 090: Owner-managed contact relation registry.
--
-- WHY THIS EXISTS
-- ---------------
-- Until now the only way to tell the system who an inbound sender is (family,
-- friend, vendor, journalist, threat …) was to edit Python or .env. Nothing in
-- the admin UI could express "this number is my brother, answer him casually"
-- or "never auto-reply to this number".
--
-- Worse, the two places that DID carry a relation were inconsistent:
--   * wbom_contacts.relation            — free text, never written by
--                                         contact_sync, DB default 'unknown';
--                                         read by identity_brain Step 4.
--   * wbom_contacts.relation_type_id    — FK to wbom_relation_types, only
--                                         written by migration 031 backfill;
--                                         read by message_router
--                                         get_contact_context().
-- Editing one never changed the other, and an unclassified contact silently
-- fell through to role='repeat_client' (fixed in 234e1d7).
--
-- fazle_contact_relations is the single, owner-editable source of truth for
-- "who is this person to me". It is deliberately SEPARATE from wbom_contacts
-- because that table is a mirror of the phone's own contact book and is
-- overwritten by the daily bridge sync — an owner decision must never be
-- destroyed by a sync.
--
-- Relationship to other identity sources (authority order, highest first):
--   ADMIN_NUMBERS (env) > ACCOUNTANT_PHONE (env) > fazle_contact_roles (seed)
--   > wbom_employees / escort roster > **fazle_contact_relations** (this table)
--   > wbom_contacts > keyword text hints > unknown
--
-- This table can only ever assert NON-PRIVILEGED roles (see
-- modules/contact_relations.RELATION_VOCABULARY): it can never grant
-- admin/accountant/employee privileges. Privilege always comes from the env
-- allowlists or the employees table, never from a UI dropdown.

BEGIN;

CREATE TABLE IF NOT EXISTS public.fazle_contact_relations (
    id             BIGSERIAL PRIMARY KEY,
    -- Canonical 8801XXXXXXXXX form (modules.phone_normalizer.normalize_phone).
    -- Matching at lookup time also accepts 01XXXXXXXXX / +8801XXXXXXXXX
    -- variants so a row written before canonicalisation still resolves.
    phone          TEXT        NOT NULL,
    -- Reserved for the multi-domain work in the portability plan; '' means
    -- "the default domain". Kept NOT NULL DEFAULT '' so the unique index
    -- below can be a plain (phone, domain_code) conflict target for upserts.
    domain_code    TEXT        NOT NULL DEFAULT '',
    -- Canonical vocabulary label, e.g. 'Family', 'Vendor', 'Threat'.
    -- Validated against modules.contact_relations.RELATION_VOCABULARY at the
    -- API layer; stored as the human-readable label for UI/audit readability.
    relation_name  TEXT        NOT NULL,
    -- How much the owner trusts this contact. Consumed by the draft/auto-reply
    -- gates; 'blocked' additionally means "never auto-reply".
    trust_level    TEXT        NOT NULL DEFAULT 'normal',
    -- inherit      → follow the existing role/config gates untouched
    -- always_draft → never auto-send, always queue a draft for approval
    -- never_reply  → stay silent entirely (no draft, no send)
    auto_reply     TEXT        NOT NULL DEFAULT 'inherit',
    -- Optional owner note used as tone guidance, e.g. "talk informally, he is
    -- my cousin" or "police — do not commit to anything".
    tone           TEXT        NOT NULL DEFAULT '',
    -- Optional display-name override. wbom_contacts.display_name keeps being
    -- synced from the phone book; this value wins for owner-facing surfaces.
    display_name   TEXT,
    notes          TEXT        NOT NULL DEFAULT '',
    is_active      BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by     TEXT        NOT NULL DEFAULT '',
    updated_by     TEXT        NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fazle_contact_relations_trust_level_chk
        CHECK (trust_level IN ('trusted', 'normal', 'caution', 'blocked')),
    CONSTRAINT fazle_contact_relations_auto_reply_chk
        CHECK (auto_reply IN ('inherit', 'always_draft', 'never_reply'))
);

COMMENT ON TABLE public.fazle_contact_relations IS
    'Owner-managed, UI-editable identity/relation registry. source of truth for '
    '"who is this WhatsApp sender". Never grants privileged roles.';

COMMENT ON COLUMN public.fazle_contact_relations.auto_reply IS
    'inherit = existing role/config gates; always_draft = queue draft only; '
    'never_reply = stay silent.';

-- One active row per phone per domain; the plain-column conflict target keeps
-- `ON CONFLICT (phone, domain_code)` usable from the API upsert.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fazle_contact_relations_phone_domain
    ON public.fazle_contact_relations (phone, domain_code);

CREATE INDEX IF NOT EXISTS idx_fazle_contact_relations_relation
    ON public.fazle_contact_relations (relation_name)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_fazle_contact_relations_updated
    ON public.fazle_contact_relations (updated_at DESC);


-- ── Relation vocabulary, mirrored into wbom_relation_types ────────────────────
-- Rows 1-6 already exist (Client, Vendor, Partner, Employee, Escort Client,
-- Family — migration 031). These 10 rows complete the vocabulary the registry
-- offers to a non-coder admin. They are seeded here (not only in code) so that
-- message_router.get_contact_context(), which reads relation_name through
-- relation_type_id, can display the same labels the owner picked.
INSERT INTO public.wbom_relation_types (relation_type_id, relation_name, description, is_active)
SELECT v.id, v.name, v.description, TRUE
  FROM (VALUES
        (7,  'Friend',           'Personal friend of the Owner — casual tone, not a client'),
        (8,  'Relative',         'Relative of the Owner — family-adjacent, casual tone'),
        (9,  'Journalist',       'Media/journalist contact — cautious, no business disclosures'),
        (10, 'Government',       'Government office/contact — formal tone'),
        (11, 'Political',        'Political contact — cautious tone'),
        (12, 'Law Enforcement',  'Police/law-enforcement contact — cautious, never commit'),
        (13, 'Contractor',       'Contractor/supplier working for the business'),
        (14, 'Personal Business','Personal (non-business) service contact — bKash agent, gas, etc.'),
        (15, 'Threat',           'Hostile/extortion contact — never auto-reply'),
        (16, 'Unknown',          'Not classified yet')
       ) AS v(id, name, description)
 WHERE NOT EXISTS (
        SELECT 1 FROM public.wbom_relation_types t WHERE t.relation_type_id = v.id
 );

-- Keep the surrogate sequence ahead of the ids inserted above, otherwise the
-- next default-valued INSERT would collide with id 7-16.
SELECT setval(
    'public.wbom_relation_types_relation_type_id_seq',
    GREATEST((SELECT COALESCE(MAX(relation_type_id), 1) FROM public.wbom_relation_types), 16)
);

COMMIT;
