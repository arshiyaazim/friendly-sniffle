-- Migration 092: first-boot admin credentials
-- ===========================================
--
-- WHY THIS EXISTS
-- ---------------
-- Until now a fresh install had NO way to log in with a username/password:
--
--   * `rbac.ensure_bootstrap_admins()` creates one `fazle_admins` row per phone
--     listed in ADMIN_NUMBERS, but leaves `username` and `password_hash` NULL.
--   * `POST /api/admin/login` in `password` mode therefore always answered
--     "Invalid username or password" on a brand-new deployment.
--   * The only working credential was the raw `INTERNAL_API_KEY` — a server
--     secret, not an account an owner can rotate by themselves.
--
-- This migration adds the two pieces the bootstrap needs:
--   * `must_change_password` — TRUE on the auto-created credential, so the
--     default password is a one-time door, not a permanent one. The login
--     response surfaces it and `POST /api/admin/change-password` clears it.
--   * `credential_set_at` — audit trail of when the account got its password.
--   * a case-insensitive unique index on `username` (the application already
--     rejected duplicates in code; this makes the database enforce it, and also
--     stops the bootstrap from colliding with an existing row).
--
-- The index is created inside a guarded block: a deployment that somehow
-- already contains duplicated usernames must not fail its migration — it gets a
-- notice instead, and the application-side duplicate check still applies.

BEGIN;

ALTER TABLE fazle_admins
    ADD COLUMN IF NOT EXISTS must_change_password boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS credential_set_at timestamptz;

COMMENT ON COLUMN public.fazle_admins.must_change_password IS
    'TRUE when the stored password is a bootstrap default and must be changed by the account owner.';
COMMENT ON COLUMN public.fazle_admins.credential_set_at IS
    'When the username/password credential was last set (bootstrap or self-service change).';

DO $$
BEGIN
    CREATE UNIQUE INDEX IF NOT EXISTS uq_fazle_admins_username_lower
        ON fazle_admins (lower(username))
        WHERE username IS NOT NULL;
EXCEPTION
    WHEN unique_violation THEN
        RAISE NOTICE 'uq_fazle_admins_username_lower not created: duplicate usernames exist';
    WHEN duplicate_table THEN
        NULL;
END $$;

COMMIT;
