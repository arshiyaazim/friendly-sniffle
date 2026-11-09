#!/usr/bin/env python3
"""Find the API tokens that cannot write safely, and fix their roles.

The audit closed the "any valid key can write" gap with a write guard
(`RBAC_WRITE_MIN_LEVEL`, see app/main.py). Turning that guard on is only safe
once every account that legitimately writes has a role — otherwise the caller
gets a 403 in the middle of a business process. This script is the step between
"audit mode" and "enforce":

  1. **find**  — active admins with *no* role, and which of them actually hold an
     API token (`api_key_hash` or `login_token_hash` set). Those are the exact
     callers that break the moment `RBAC_WRITE_MIN_LEVEL > 0`.
  2. **grant** — assign a role to one account, or to every roleless account, with
     `--dry-run` first and an explicit `--yes` for the bulk form.
  3. **verify** — re-run without `--grant`: `--strict` exits 1 while a roleless
     active admin remains, so it can gate a deploy.

Usage:
    python scripts/rbac_write_audit.py
    python scripts/rbac_write_audit.py --json
    python scripts/rbac_write_audit.py --strict           # exit 1 if roleless active admins exist
    python scripts/rbac_write_audit.py --grant viewer --username admin --dry-run
    python scripts/rbac_write_audit.py --grant operator --phone 8801700000001
    python scripts/rbac_write_audit.py --grant viewer --all-roleless --yes
    python scripts/rbac_write_audit.py --selftest         # no database needed

Exit codes:
    0  nobody is roleless (or the grant succeeded)
    1  roleless active admins remain and --strict was passed
    2  cannot connect / bad arguments
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ARGS = sys.argv[1:]
SELFTEST = "--selftest" in ARGS
AS_JSON = "--json" in ARGS
STRICT = "--strict" in ARGS
DRY_RUN = "--dry-run" in ARGS
CONFIRMED = "--yes" in ARGS
GRANT_ALL = "--all-roleless" in ARGS
GRANT_ROLE = _role = None


def _arg(name: str) -> str | None:
    if name in ARGS:
        idx = ARGS.index(name)
        if idx + 1 < len(ARGS):
            return ARGS[idx + 1]
    return None


GRANT_ROLE = _arg("--grant")
GRANT_PHONE = _arg("--phone")
GRANT_USERNAME = _arg("--username")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── pure helpers (tested without a database) ────────────────────────────────
def classify(rows: list[dict]) -> dict:
    """Split admin rows into the groups the report needs.

    Input rows: {id, phone, username, status, has_token, max_level, roles}
    """
    roleless_active = [r for r in rows
                       if (r.get("status") or "active") == "active"
                       and int(r.get("max_level") or 0) <= 0]
    roleless_with_token = [r for r in roleless_active if r.get("has_token")]
    disabled = [r for r in rows if (r.get("status") or "active") != "active"]
    privileged = [r for r in rows if int(r.get("max_level") or 0) > 0]
    return {
        "total": len(rows),
        "roleless_active": roleless_active,
        "roleless_with_token": roleless_with_token,
        "disabled": disabled,
        "privileged": privileged,
    }


def grant_plan(rows: list[dict], *, role: str, phone: str | None,
               username: str | None, all_roleless: bool) -> list[dict]:
    """Which admin rows a --grant invocation would change."""
    if not role:
        return []
    if all_roleless:
        targets = [r for r in rows
                   if (r.get("status") or "active") == "active"
                   and int(r.get("max_level") or 0) <= 0]
    elif phone:
        targets = [r for r in rows if (r.get("phone") or "") == phone]
    elif username:
        targets = [r for r in rows if (r.get("username") or "") == username]
    else:
        targets = []
    # already holds this role → nothing to do
    return [r for r in targets if role not in (r.get("roles") or [])]


def _label(row: dict) -> str:
    name = row.get("username") or row.get("name") or ""
    phone = row.get("phone") or "(no phone)"
    return f"{name} / {phone} (#{row.get('id')})"


# ── database ────────────────────────────────────────────────────────────────
async def _load(conn) -> tuple[list[dict], list[dict]]:
    roles = [dict(r) for r in await conn.fetch(
        "SELECT name, level FROM fazle_roles ORDER BY level DESC")]
    rows = [dict(r) for r in await conn.fetch(
        """
        SELECT a.id, a.phone, a.name, a.username, COALESCE(a.status, 'active') AS status,
               (a.api_key_hash IS NOT NULL OR a.login_token_hash IS NOT NULL) AS has_token,
               COALESCE(MAX(r.level), 0) AS max_level,
               COALESCE(ARRAY_AGG(ar.role_name) FILTER (WHERE ar.role_name IS NOT NULL),
                        ARRAY[]::text[]) AS roles
          FROM fazle_admins a
          LEFT JOIN fazle_admin_roles ar ON ar.admin_id = a.id
          LEFT JOIN fazle_roles r        ON r.name = ar.role_name
         GROUP BY a.id
         ORDER BY a.id
        """)]
    return rows, roles


async def _apply_grants(conn, plan: list[dict], role: str) -> int:
    applied = 0
    for row in plan:
        await conn.execute(
            """
            INSERT INTO fazle_admin_roles (admin_id, role_name, granted_by)
            VALUES ($1, $2, 'rbac_write_audit')
            ON CONFLICT (admin_id, role_name) DO NOTHING
            """,
            row["id"], role,
        )
        applied += 1
    return applied


async def run() -> int:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is required (or --selftest)", file=sys.stderr)
        return 2
    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg missing — run inside the service venv", file=sys.stderr)
        return 2
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot connect: {exc}", file=sys.stderr)
        return 2

    try:
        rows, roles = await _load(conn)
        groups = classify(rows)

        if GRANT_ROLE:
            known = {r["name"] for r in roles}
            if GRANT_ROLE not in known:
                print(f"ERROR: unknown role {GRANT_ROLE!r}; roles are: "
                      f"{', '.join(sorted(known)) or '(none seeded)'}", file=sys.stderr)
                return 2
            plan = grant_plan(rows, role=GRANT_ROLE, phone=GRANT_PHONE,
                              username=GRANT_USERNAME, all_roleless=GRANT_ALL)
            if not plan:
                print("nothing to grant — no matching roleless/roleless-matching account")
                return 0
            for row in plan:
                level = next((r["level"] for r in roles if r["name"] == GRANT_ROLE), "?")
                print(f"{'would grant' if DRY_RUN else 'granting'} {GRANT_ROLE} "
                      f"(level {level}) to {_label(row)}")
            if DRY_RUN:
                print(f"\ndry run: {len(plan)} account(s) unchanged")
                return 0
            if GRANT_ALL and not CONFIRMED:
                print("\nrefusing to modify every roleless account without --yes",
                      file=sys.stderr)
                return 2
            changed = await _apply_grants(conn, plan, GRANT_ROLE)
            print(f"\ngranted {GRANT_ROLE} to {changed} account(s)")
            rows, roles = await _load(conn)
            groups = classify(rows)

        if AS_JSON:
            print(json.dumps({
                "roles": roles,
                "total_admins": groups["total"],
                "roleless_active": [
                    {"id": r["id"], "phone": r["phone"], "username": r["username"],
                     "has_token": bool(r["has_token"])}
                    for r in groups["roleless_active"]
                ],
                "roleless_with_token": len(groups["roleless_with_token"]),
                "disabled": len(groups["disabled"]),
            }, indent=2))
        else:
            print("roles")
            for r in roles:
                print(f"  {r['level']:>4}  {r['name']}")
            print(f"\nadmins: {groups['total']} total, "
                  f"{len(groups['privileged'])} with a role, "
                  f"{len(groups['disabled'])} disabled")
            if not groups["roleless_active"]:
                print("\n✓ every active admin has at least one role — "
                      "RBAC_WRITE_MIN_LEVEL can be raised safely")
            else:
                print(f"\n!! {len(groups['roleless_active'])} active admin(s) have NO role:")
                for r in groups["roleless_active"]:
                    token = "holds an API token" if r["has_token"] else "no token"
                    print(f"   {_label(r)}  — {token}")
                if groups["roleless_with_token"]:
                    print(f"\n   {len(groups['roleless_with_token'])} of them hold a token: "
                          "these are the callers that break when the write guard is enforced.")
                print("\n   fix: python scripts/rbac_write_audit.py "
                      "--grant viewer --username <name>   (or --all-roleless --yes)")
                print("   then: RBAC_WRITE_MIN_LEVEL=10 in .env and restart")

        if STRICT and groups["roleless_active"]:
            return 1
        return 0
    finally:
        await conn.close()


def _selftest() -> int:
    rows = [
        {"id": 1, "phone": "880170000001", "username": "admin", "status": "active",
         "has_token": True, "max_level": 100, "roles": ["superadmin"]},
        {"id": 2, "phone": "880170000002", "username": "ops", "status": "active",
         "has_token": True, "max_level": 0, "roles": []},
        {"id": 3, "phone": "880170000003", "username": "old", "status": "disabled",
         "has_token": True, "max_level": 0, "roles": []},
        {"id": 4, "phone": "880170000004", "username": None, "status": "active",
         "has_token": False, "max_level": 10, "roles": ["viewer"]},
    ]
    groups = classify(rows)
    assert groups["total"] == 4
    assert [r["id"] for r in groups["roleless_active"]] == [2], groups
    assert [r["id"] for r in groups["roleless_with_token"]] == [2]
    assert [r["id"] for r in groups["disabled"]] == [3]
    assert [r["id"] for r in groups["privileged"]] == [1, 4]

    plan = grant_plan(rows, role="viewer", phone=None, username="ops", all_roleless=False)
    assert [r["id"] for r in plan] == [2]
    # an explicit target may carry several roles: viewer is not yet held → addable
    assert [r["id"] for r in grant_plan(rows, role="viewer", phone=None,
                                       username="admin", all_roleless=False)] == [1]
    # …but the same role twice is a no-op (ON CONFLICT would swallow it anyway)
    assert grant_plan(rows, role="superadmin", phone=None, username="admin",
                      all_roleless=False) == []
    # bulk only touches active roleless accounts
    assert [r["id"] for r in grant_plan(rows, role="viewer", phone=None, username=None,
                                        all_roleless=True)] == [2]
    # no arguments → no plan (never grant by accident)
    assert grant_plan(rows, role="viewer", phone=None, username=None, all_roleless=False) == []
    assert _label(rows[0]).startswith("admin / 880170000001")
    print("selftest ok")
    return 0


if __name__ == "__main__":
    if SELFTEST:
        raise SystemExit(_selftest())
    raise SystemExit(asyncio.run(run()))
