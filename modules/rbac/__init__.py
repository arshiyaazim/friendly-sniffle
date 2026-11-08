"""Batch 19 — RBAC & Audit module.

Tables
------
* ``fazle_roles`` (seeded: viewer / operator / accountant / admin / superadmin)
* ``fazle_admins`` — humans who can act on the system
* ``fazle_admin_roles`` — admin↔role mapping
* ``fazle_admin_audit`` — every command attempt (allowed or denied)

Concepts
--------
* Each command has a *required_role*. The admin's max role-level must be ≥
  that command's level.
* Phones listed in env ``ADMIN_NUMBERS`` (B11/B12) are auto-bootstrapped on
  first sight as ``superadmin`` so the system is never locked out.
* API keys are stored as SHA-256 hashes; lookup is by hash.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
from typing import Any, Optional

from app.database import execute, fetch_all, fetch_one, fetch_val

log = logging.getLogger("fazle.rbac")


# ── command → required role table ────────────────────────────────────────────
COMMAND_ROLE: dict[str, str] = {
    # viewer (read-only)
    "status":              "viewer",
    "schedule_status":     "viewer",
    "report_list":         "viewer",
    "report_daily":        "viewer",
    "report_payroll":      "viewer",
    "report_cash":         "viewer",
    "report_recon":        "viewer",
    "report_escort":       "viewer",
    "backup_status":       "viewer",
    "backup_list":         "viewer",
    "payroll_list":        "viewer",
    # office_assistant (view attendance list/own records + create draft entries only)
    "attendance_draft_submit": "office_assistant",
    # operator
    "approve":             "operator",
    "reject":              "operator",
    "edit":                "operator",
    "paid":                "operator",
    "advance":             "operator",
    "release":             "operator",
    "escortconfirm":       "operator",
    # accountant
    "payimport":           "accountant",
    "payroll_compute":     "accountant",
    "payroll_trans":       "accountant",
    "payroll_paid":        "accountant",
    "payroll_cancel":      "accountant",
    # admin
    "schedule_run":        "admin",
    "backup_now":          "admin",
    "user_list":           "admin",
    # Step 4 — latest-approved Recruitment authority control plane
    "recruitment_authority_read": "viewer",
    "recruitment_authority_write": "admin",
    "recruitment_authority_approve": "admin",
    # superadmin
    "user_add":            "superadmin",
    "user_role":           "superadmin",
    "user_remove":         "superadmin",
    "user_apikey":         "superadmin",
    # Phase 4 (2026-08-04): WhatsApp->Hermes relay -- superadmin only, on
    # top of the bridge2-specific check in admin_directives/router.py
    # (two independent gates: role AND channel, not either alone).
    "hermes_relay":        "superadmin",
    # Hermes Capability Expansion Level 2 (2026-08-10): admin-commanded,
    # one-off WhatsApp send via POST /admin/send-whatsapp -- same tier as
    # hermes_relay above, the closest existing precedent for "an AI-adjacent
    # action with a real external effect." Deliberately explicit here: an
    # unlisted command silently falls back to DEFAULT_REQUIRED_ROLE ("admin"),
    # which would be too permissive for something that sends a real message
    # to a real person.
    "send_whatsapp_message": "superadmin",
    # modules.hermes_tasks (2026-08-19, persistent Task/Execution State +
    # diff-level Action Approval). task_write covers create/claim/status-
    # update/propose_action/record_execution_result -- Earth's own planning
    # bookkeeping, never itself an approved action being executed, same
    # tier as the existing "operator" write commands above. task_approve
    # is the one that actually authorizes a proposed file mutation/git
    # commit/deploy/migration/restart/production write/opencode_dispatch
    # call to proceed -- same superadmin tier as send_whatsapp_message,
    # the closest existing precedent for "an AI-adjacent action with a
    # real external effect."
    "task_read":           "viewer",
    "task_write":          "operator",
    "task_approve":        "superadmin",
    # Stage H5-H8 (2026-08-28): CASH APPROVE/REJECT/REVERSE trusted-admin
    # WhatsApp commands -- same tier as _require_admin_role (the existing
    # HTTP-route guard on operator_approve/operator_reject/reverse_txn in
    # modules.fazle_payroll_engine.routes) for approve/reject. REVERSE is
    # deliberately one tier higher (superadmin, same as user_add/
    # user_role/user_remove/hermes_relay/send_whatsapp_message) -- H8's
    # own framing ("highest risk... NO standing autonomous reverse") is
    # the closest match to those existing superadmin-only, real-external-
    # effect commands, not the ordinary admin tier.
    "cash_approve":        "admin",
    "cash_reject":         "admin",
    "cash_reverse":        "superadmin",
    # Stage I5 (2026-08-29): read-only inspection -- same tier as the
    # other read-only cash/report commands (report_cash="viewer"), not
    # the mutation tier.
    "cash_details":        "viewer",
    # Exact inbound provenance recovery; metadata/queue finalization only.
    "inbound_recovery":    "admin",
    # Stage J7 (2026-08-29): same tier as the existing task_approve
    # (superadmin) for authorizing a proposal's execution; reject/details
    # mirror cash_reject/cash_details' own tiers.
    "proposal_approve":    "superadmin",
    "proposal_reject":     "admin",
    "proposal_details":    "viewer",
}

DEFAULT_REQUIRED_ROLE = "admin"  # unknown cmd defaults to admin


# ── helpers ──────────────────────────────────────────────────────────────────
def _normalize_phone(p: str) -> str:
    from modules.phone_normalizer import normalize_phone as _pn
    return _pn(p or "") or re.sub(r"\D", "", p or "")


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 200_000
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    hash_b64 = base64.urlsafe_b64encode(derived).decode("ascii")
    return f"pbkdf2_sha256${iterations}${salt_b64}${hash_b64}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations_s, salt_b64, hash_b64 = (stored_hash or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(hash_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def new_login_token() -> str:
    return "fk_" + secrets.token_urlsafe(32)


def _bootstrap_phones() -> list[str]:
    raw = os.getenv("ADMIN_NUMBERS", "")
    out = []
    for p in re.split(r"[,;\s]+", raw):
        p = _normalize_phone(p)
        if p:
            out.append(p)
    return out


# ── core lookups ─────────────────────────────────────────────────────────────
async def get_admin_by_phone(phone: str) -> Optional[dict]:
    p = _normalize_phone(phone)
    if not p:
        return None
    row = await fetch_one(
        "SELECT id, phone, name, username, status FROM fazle_admins WHERE phone=$1", p,
    )
    return dict(row) if row else None


async def get_admin_by_api_key(key: str) -> Optional[dict]:
    if not key:
        return None
    h = hash_api_key(key)
    row = await fetch_one(
        "SELECT id, phone, name, username, status FROM fazle_admins WHERE api_key_hash=$1 OR login_token_hash=$2",
        h, h,
    )
    return dict(row) if row else None


async def get_admin_by_username(username: str) -> Optional[dict]:
    u = _normalize_username(username)
    if not u:
        return None
    try:
        row = await fetch_one(
            "SELECT id, phone, name, status, username, password_hash, must_change_password "
            "FROM fazle_admins WHERE LOWER(username)=$1",
            u,
        )
    except Exception:
        # pre-092 schema (no must_change_password column yet)
        row = await fetch_one(
            "SELECT id, phone, name, status, username, password_hash "
            "FROM fazle_admins WHERE LOWER(username)=$1",
            u,
        )
    return dict(row) if row else None


async def get_roles(admin_id: int) -> list[str]:
    rows = await fetch_all(
        "SELECT r.name FROM fazle_admin_roles ar "
        "JOIN fazle_roles r ON r.name = ar.role_name "
        "WHERE ar.admin_id = $1 ORDER BY r.level DESC",
        admin_id,
    )
    return [r["name"] for r in rows]


async def get_role_level(role_name: str) -> int:
    v = await fetch_val("SELECT level FROM fazle_roles WHERE name=$1", role_name)
    return int(v) if v is not None else 0


async def max_role_level(admin_id: int) -> int:
    v = await fetch_val(
        "SELECT COALESCE(MAX(r.level), 0) FROM fazle_admin_roles ar "
        "JOIN fazle_roles r ON r.name = ar.role_name WHERE ar.admin_id=$1",
        admin_id,
    )
    return int(v or 0)


# ── bootstrap ────────────────────────────────────────────────────────────────

DEFAULT_BOOTSTRAP_USERNAME = "admin"
DEFAULT_BOOTSTRAP_PHONE = "8800000000000"   # placeholder only when ADMIN_NUMBERS is empty


def bootstrap_credential_env() -> tuple[str, str]:
    """(username, password) from the environment. Password may be "" => generate."""
    user = _normalize_username(os.getenv("BOOTSTRAP_ADMIN_USERNAME") or DEFAULT_BOOTSTRAP_USERNAME)
    if not user:
        user = DEFAULT_BOOTSTRAP_USERNAME
    return user, (os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or "").strip()


def generate_bootstrap_password() -> str:
    """Readable one-time password (no ambiguous characters, 18 chars)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(18))


async def _any_credential_exists() -> bool:
    try:
        row = await fetch_one(
            "SELECT 1 AS ok FROM fazle_admins "
            "WHERE username IS NOT NULL AND password_hash IS NOT NULL LIMIT 1"
        )
        return bool(row)
    except Exception:
        return True  # fail closed: never overwrite credentials on a lookup error


async def _first_superadmin_id() -> Optional[int]:
    try:
        v = await fetch_val(
            "SELECT a.id FROM fazle_admins a "
            "JOIN fazle_admin_roles ar ON ar.admin_id = a.id "
            "WHERE ar.role_name = 'superadmin' AND a.status = 'active' "
            "ORDER BY a.id LIMIT 1"
        )
        if v is not None:
            return int(v)
    except Exception:
        pass
    try:
        v = await fetch_val(
            "SELECT id FROM fazle_admins WHERE status='active' ORDER BY id LIMIT 1"
        )
        return int(v) if v is not None else None
    except Exception:
        return None


async def _create_owner_admin() -> int:
    """Create the very first admin when none exists (empty ADMIN_NUMBERS)."""
    raw_phone = ""
    phones = _bootstrap_phones()
    if phones:
        raw_phone = phones[0]
    else:
        raw_phone = (os.getenv("BOOTSTRAP_ADMIN_PHONE") or DEFAULT_BOOTSTRAP_PHONE).strip()
    phone = _normalize_phone(raw_phone) or DEFAULT_BOOTSTRAP_PHONE
    admin_id = await fetch_val(
        "INSERT INTO fazle_admins (phone, name, status, notes) "
        "VALUES ($1, 'Owner', 'active', 'auto-bootstrap (first install)') RETURNING id",
        phone,
    )
    try:
        role_exists = await fetch_val("SELECT 1 FROM fazle_roles WHERE name='superadmin'")
        if role_exists:
            await execute(
                "INSERT INTO fazle_admin_roles (admin_id, role_name, granted_by) "
                "VALUES ($1,'superadmin','bootstrap') ON CONFLICT DO NOTHING",
                admin_id,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[rbac] could not grant superadmin to bootstrap admin: {exc}")
    return int(admin_id)


async def ensure_bootstrap_credentials() -> dict:
    """Guarantee that a first install has a working username/password login.

    Idempotent and non-destructive:

      * if ANY admin already has a username + password, nothing is touched;
      * otherwise the first superadmin (or the first active admin, or a newly
        created owner row) receives BOOTSTRAP_ADMIN_USERNAME /
        BOOTSTRAP_ADMIN_PASSWORD. When no password is configured one is
        generated and printed ONCE in the startup log;
      * the credential is marked `must_change_password = true`, which the login
        response surfaces and POST /api/admin/change-password clears.

    Returns a dict describing what happened. Never raises.
    """
    out: dict = {"status": "noop", "username": None, "generated_password": None}
    try:
        if await _any_credential_exists():
            out["status"] = "already_configured"
            return out

        username, password = bootstrap_credential_env()
        generated = False
        if not password:
            password = generate_bootstrap_password()
            generated = True
        if len(password) < 6:
            log.warning("[rbac] BOOTSTRAP_ADMIN_PASSWORD is shorter than 6 characters — ignoring it")
            password = generate_bootstrap_password()
            generated = True

        admin_id = await _first_superadmin_id()
        if admin_id is None:
            admin_id = await _create_owner_admin()

        # The username must be free (or already belong to this same row).
        try:
            holder = await fetch_val(
                "SELECT id FROM fazle_admins WHERE LOWER(username)=$1 AND id <> $2",
                username, admin_id,
            )
            if holder:
                fallback = f"{username}{admin_id}"
                log.warning(f"[rbac] username {username!r} is taken — using {fallback!r}")
                username = fallback
        except Exception:
            pass

        try:
            await execute(
                "UPDATE fazle_admins SET username=$1, password_hash=$2, "
                "must_change_password=true, credential_set_at=NOW(), status='active' "
                "WHERE id=$3",
                username, hash_password(password), admin_id,
            )
        except Exception:
            # pre-092 schema: no must_change_password / credential_set_at columns
            await execute(
                "UPDATE fazle_admins SET username=$1, password_hash=$2, status='active' "
                "WHERE id=$3",
                username, hash_password(password), admin_id,
            )

        # The first-boot account must actually be an owner: migration 093 seeds
        # the role catalogue, and without the grant a fresh install could log in
        # but not administer anything.
        try:
            granted = await fetch_val(
                "SELECT 1 FROM fazle_admin_roles WHERE admin_id=$1 AND role_name='superadmin'",
                admin_id,
            )
            if not granted and await fetch_val(
                "SELECT 1 FROM fazle_roles WHERE name='superadmin'"
            ):
                await execute(
                    "INSERT INTO fazle_admin_roles (admin_id, role_name, granted_by) "
                    "VALUES ($1,'superadmin','bootstrap_credentials') "
                    "ON CONFLICT DO NOTHING",
                    admin_id,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[rbac] could not grant superadmin to {username!r}: {exc}")

        out.update(
            status="created",
            admin_id=admin_id,
            username=username,
            generated_password=password if generated else None,
        )
        if generated:
            log.warning(
                "\n"
                "==================================================================\n"
                "  FIRST-BOOT ADMIN LOGIN (shown once)\n"
                "    username : %s\n"
                "    password : %s\n"
                '  Log in via POST /api/admin/login {"mode":"password"}\n'
                "  then change it: POST /api/admin/change-password\n"
                "  (or set BOOTSTRAP_ADMIN_USERNAME/PASSWORD and delete the account's\n"
                "   password before restarting to re-bootstrap deliberately)\n"
                "==================================================================",
                username, password,
            )
        else:
            log.info(
                "[rbac] bootstrap credential created for %s (password from BOOTSTRAP_ADMIN_PASSWORD)",
                username,
            )
        return out
    except Exception as exc:  # noqa: BLE001 — bootstrap must never block startup
        log.warning(f"[rbac] bootstrap credential skipped: {exc}")
        out["status"] = "error"
        out["error"] = str(exc)[:200]
        return out


async def set_own_password(admin_id: int, new_password: str) -> None:
    """Self-service password change; clears must_change_password."""
    try:
        await execute(
            "UPDATE fazle_admins SET password_hash=$1, must_change_password=false, "
            "credential_set_at=NOW() WHERE id=$2",
            hash_password(new_password), int(admin_id),
        )
    except Exception:
        await execute(
            "UPDATE fazle_admins SET password_hash=$1 WHERE id=$2",
            hash_password(new_password), int(admin_id),
        )


async def ensure_bootstrap_admins() -> int:
    """Create env ADMIN_NUMBERS entries as superadmin if missing."""
    created = 0
    for phone in _bootstrap_phones():
        existing = await get_admin_by_phone(phone)
        if existing:
            # ensure they are superadmin
            roles = await get_roles(existing["id"])
            if "superadmin" not in roles:
                await execute(
                    "INSERT INTO fazle_admin_roles (admin_id, role_name, granted_by) "
                    "VALUES ($1,'superadmin','bootstrap') "
                    "ON CONFLICT DO NOTHING",
                    existing["id"],
                )
                log.info(f"[rbac] promoted bootstrap admin {phone} → superadmin")
            continue
        admin_id = await fetch_val(
            "INSERT INTO fazle_admins (phone, name, status, notes) "
            "VALUES ($1, $2, 'active', 'auto-bootstrap from ADMIN_NUMBERS') "
            "RETURNING id",
            phone, f"bootstrap_{phone[-4:]}",
        )
        await execute(
            "INSERT INTO fazle_admin_roles (admin_id, role_name, granted_by) "
            "VALUES ($1,'superadmin','bootstrap')",
            admin_id,
        )
        created += 1
        log.info(f"[rbac] bootstrapped {phone} as superadmin (id={admin_id})")
    return created


# ── permission check ─────────────────────────────────────────────────────────
async def check_permission(
    *, phone: Optional[str] = None,
    api_key: Optional[str] = None,
    command: str,
) -> dict[str, Any]:
    """Return {allowed, admin, required_role, reason}."""
    required = COMMAND_ROLE.get(command, DEFAULT_REQUIRED_ROLE)
    req_level = await get_role_level(required)

    admin: Optional[dict] = None
    if phone:
        admin = await get_admin_by_phone(phone)
    if not admin and api_key:
        admin = await get_admin_by_api_key(api_key)

    if not admin:
        return {
            "allowed": False, "admin": None,
            "required_role": required, "reason": "unknown actor",
        }
    if admin["status"] != "active":
        return {
            "allowed": False, "admin": admin,
            "required_role": required, "reason": f"actor status={admin['status']}",
        }
    user_level = await max_role_level(admin["id"])
    if user_level < req_level:
        return {
            "allowed": False, "admin": admin,
            "required_role": required,
            "reason": f"need {required} (level {req_level}) have level {user_level}",
        }
    # touch last_seen_at (best effort)
    try:
        await execute(
            "UPDATE fazle_admins SET last_seen_at=now() WHERE id=$1", admin["id"],
        )
    except Exception:
        pass
    return {
        "allowed": True, "admin": admin,
        "required_role": required, "reason": "",
    }


# ── audit ────────────────────────────────────────────────────────────────────
async def record_audit(
    *, channel: str, command: str,
    actor_phone: Optional[str] = None,
    actor_admin: Optional[dict] = None,
    args: Optional[str] = None,
    allowed: bool,
    required_role: Optional[str] = None,
    denied_reason: Optional[str] = None,
    result_summary: Optional[str] = None,
) -> int:
    label = (actor_admin or {}).get("name") or actor_phone or "unknown"
    aid = (actor_admin or {}).get("id")
    rid = await fetch_val(
        "INSERT INTO fazle_admin_audit (actor_phone, actor_user_id, actor_label, "
        "channel, command, args, allowed, required_role, denied_reason, result_summary) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id",
        actor_phone, aid, label, channel, command,
        (args[:4000] if args else None),
        allowed, required_role, denied_reason,
        (result_summary[:4000] if result_summary else None),
    )
    return int(rid)


async def record_audit_conn(
    conn: Any, *, channel: str, command: str,
    actor_phone: Optional[str] = None, actor_admin: Optional[dict] = None,
    args: Optional[str] = None, allowed: bool, required_role: Optional[str] = None,
    denied_reason: Optional[str] = None, result_summary: Optional[str] = None,
) -> int:
    """Write an audit event on a caller-owned transaction connection."""
    label = (actor_admin or {}).get("name") or actor_phone or "unknown"
    aid = (actor_admin or {}).get("id")
    rid = await conn.fetchval(
        "INSERT INTO fazle_admin_audit (actor_phone, actor_user_id, actor_label, "
        "channel, command, args, allowed, required_role, denied_reason, result_summary) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id",
        actor_phone, aid, label, channel, command, (args[:4000] if args else None),
        allowed, required_role, denied_reason, (result_summary[:4000] if result_summary else None),
    )
    return int(rid)


async def list_audit(limit: int = 50, command: Optional[str] = None) -> list[dict]:
    if command:
        rows = await fetch_all(
            "SELECT * FROM fazle_admin_audit WHERE command=$1 "
            "ORDER BY created_at DESC LIMIT $2",
            command, limit,
        )
    else:
        rows = await fetch_all(
            "SELECT * FROM fazle_admin_audit ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]


# ── admin (user) management ──────────────────────────────────────────────────
async def add_admin(
    phone: str,
    name: str,
    role: str = "viewer",
    granted_by: str = "system",
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> dict[str, Any]:
    p = _normalize_phone(phone)
    if not p:
        raise ValueError("invalid phone")
    u = _normalize_username(username) if username else None
    if bool(u) != bool(password):
        raise ValueError("username and password must be provided together")
    if u:
        existing_username = await fetch_val("SELECT 1 FROM fazle_admins WHERE LOWER(username)=$1", u)
        if existing_username:
            raise ValueError(f"username already exists: {u}")
    exists = await get_admin_by_phone(p)
    if exists:
        if u and password:
            existing_username = _normalize_username(exists.get("username") or "")
            if existing_username and existing_username != u:
                raise ValueError("existing admin has different username")
            owner_id = await fetch_val("SELECT id FROM fazle_admins WHERE LOWER(username)=$1", u)
            if owner_id and int(owner_id) != int(exists["id"]):
                raise ValueError(f"username already exists: {u}")
            await execute(
                "UPDATE fazle_admins SET username=$1, password_hash=$2 WHERE id=$3",
                u,
                hash_password(password),
                exists["id"],
            )
            return {"status": "updated_credentials", "admin_id": exists["id"]}
        return {"status": "exists", "admin_id": exists["id"]}
    password_hash = hash_password(password) if password else None
    aid = await fetch_val(
        "INSERT INTO fazle_admins (phone, name, username, password_hash, status) "
        "VALUES ($1,$2,$3,$4,'active') RETURNING id",
        p, name, u, password_hash,
    )
    await execute(
        "INSERT INTO fazle_admin_roles (admin_id, role_name, granted_by) "
        "VALUES ($1,$2,$3)",
        aid, role, granted_by,
    )
    return {"status": "created", "admin_id": aid, "role": role}


async def set_role(
    phone: str, role: str, granted_by: str = "system",
) -> dict[str, Any]:
    a = await get_admin_by_phone(phone)
    if not a:
        raise ValueError("unknown admin")
    # validate role exists
    if not await fetch_val("SELECT 1 FROM fazle_roles WHERE name=$1", role):
        raise ValueError(f"unknown role: {role}")
    await execute(
        "INSERT INTO fazle_admin_roles (admin_id, role_name, granted_by) "
        "VALUES ($1,$2,$3) ON CONFLICT (admin_id, role_name) DO NOTHING",
        a["id"], role, granted_by,
    )
    return {"status": "ok", "admin_id": a["id"], "role": role}


async def revoke_role(phone: str, role: str) -> dict[str, Any]:
    a = await get_admin_by_phone(phone)
    if not a:
        raise ValueError("unknown admin")
    await execute(
        "DELETE FROM fazle_admin_roles WHERE admin_id=$1 AND role_name=$2",
        a["id"], role,
    )
    return {"status": "ok", "admin_id": a["id"], "role": role}


async def disable_admin(phone: str) -> dict[str, Any]:
    a = await get_admin_by_phone(phone)
    if not a:
        raise ValueError("unknown admin")
    await execute("UPDATE fazle_admins SET status='disabled' WHERE id=$1", a["id"])
    return {"status": "ok", "admin_id": a["id"]}


async def enable_admin(phone: str) -> dict[str, Any]:
    a = await get_admin_by_phone(phone)
    if not a:
        raise ValueError("unknown admin")
    await execute("UPDATE fazle_admins SET status='active' WHERE id=$1", a["id"])
    return {"status": "ok", "admin_id": a["id"]}


async def issue_api_key(phone: str) -> dict[str, Any]:
    """Generate a new API key, store its hash, return the plaintext (once)."""
    a = await get_admin_by_phone(phone)
    if not a:
        raise ValueError("unknown admin")
    key = "fk_" + secrets.token_urlsafe(32)
    h = hash_api_key(key)
    await execute(
        "UPDATE fazle_admins SET api_key_hash=$1 WHERE id=$2", h, a["id"],
    )
    return {"status": "ok", "admin_id": a["id"], "api_key": key}


async def list_admins() -> list[dict]:
    rows = await fetch_all(
        "SELECT a.id, a.phone, a.name, a.username, a.status, a.created_at, a.last_seen_at, "
        "       COALESCE(string_agg(ar.role_name, ',' ORDER BY r.level DESC), '') AS roles "
        "FROM fazle_admins a "
        "LEFT JOIN fazle_admin_roles ar ON ar.admin_id = a.id "
        "LEFT JOIN fazle_roles r ON r.name = ar.role_name "
        "GROUP BY a.id ORDER BY a.id"
    )
    return [dict(r) for r in rows]
