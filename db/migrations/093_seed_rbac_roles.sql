-- Migration 093: seed the RBAC role catalogue
-- ============================================
--
-- WHY THIS EXISTS
-- ---------------
-- `fazle_roles` and `fazle_admin_roles` are created by migration `029`, but the
-- role *rows* were only ever seeded by hand in the original deployment. Two
-- consequences on any other database:
--
--   * `modules/rbac.create_admin(..., role=...)` refuses every role, because the
--     row is missing (`SELECT 1 FROM fazle_roles WHERE name=$1`);
--   * the first-boot account (migration 092) is created without `superadmin`, so
--     it can log in but is not an owner.
--
-- The hierarchy below is the one the code and migration `043` already assume:
--
--     viewer(10) < office_assistant(20) < operator(30) < accountant(50)
--                < admin(70) < superadmin(100)
--
-- Purely additive: `ON CONFLICT (name) DO NOTHING`, existing levels are never
-- rewritten (an operator may have raised one deliberately), and no account's
-- role assignment is touched.

BEGIN;

INSERT INTO fazle_roles (name, level, description) VALUES
    ('viewer',           10, 'Read-only access to operational status'),
    ('office_assistant', 20, 'View attendance list/own records; create attendance draft entries only'),
    ('operator',         30, 'Approve/reject drafts and edit operational records'),
    ('accountant',       50, 'Payroll, cash, income and payment reconciliation'),
    ('admin',            70, 'Administrative control over users and operational configuration'),
    ('superadmin',      100, 'Full owner access, including admin management')
ON CONFLICT (name) DO NOTHING;

COMMIT;
