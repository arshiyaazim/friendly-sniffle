"""Role coverage: find the tokens that would lose write access, and fix them.

Roadmap item 2 is *audit mode → enforcement*. `write_identity_guard` already
refuses a token whose account has no role once `RBAC_WRITE_MIN_LEVEL > 0`; the
missing half was knowing **who** that is before the switch is flipped.

These tests cover the three pieces:

  * the pure planning helpers in ``scripts/rbac_write_audit.py`` (no database);
  * the startup report in ``app.main._log_rbac_role_coverage`` (no database);
  * the real SQL against the test database, so the "roleless with a token"
    query cannot drift away from the schema.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "rbac_write_audit.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rbac_write_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)  # module guards its CLI behind __main__
    return module


@pytest.fixture(scope="module")
def audit():
    return _load_script()


@pytest.fixture(scope="module")
def main_module():
    import app.main as main
    return main


def _rows():
    return [
        {"id": 1, "phone": "8801700000001", "username": "admin", "name": "Owner",
         "status": "active", "has_token": True, "max_level": 100, "roles": ["superadmin"]},
        {"id": 2, "phone": "8801700000002", "username": "ops", "name": "Ops",
         "status": "active", "has_token": True, "max_level": 0, "roles": []},
        {"id": 3, "phone": "8801700000003", "username": "old", "name": "Old",
         "status": "disabled", "has_token": True, "max_level": 0, "roles": []},
        {"id": 4, "phone": "8801700000004", "username": None, "name": "Viewer",
         "status": "active", "has_token": False, "max_level": 10, "roles": ["viewer"]},
    ]


# ── pure helpers ────────────────────────────────────────────────────────────
def test_classify_separates_the_four_groups(audit):
    groups = audit.classify(_rows())
    assert groups["total"] == 4
    assert [r["id"] for r in groups["roleless_active"]] == [2]
    assert [r["id"] for r in groups["roleless_with_token"]] == [2]
    assert [r["id"] for r in groups["disabled"]] == [3]
    assert [r["id"] for r in groups["privileged"]] == [1, 4]


def test_disabled_accounts_are_not_reported_as_a_write_risk(audit):
    """A disabled roleless admin cannot authenticate, so it must not count."""
    groups = audit.classify(_rows())
    assert 3 not in [r["id"] for r in groups["roleless_active"]]


def test_a_missing_status_column_defaults_to_active(audit):
    groups = audit.classify([{"id": 7, "status": None, "max_level": 0, "has_token": True}])
    assert [r["id"] for r in groups["roleless_active"]] == [7]


def test_grant_plan_targets_one_account(audit):
    plan = audit.grant_plan(_rows(), role="viewer", phone="8801700000002",
                            username=None, all_roleless=False)
    assert [r["id"] for r in plan] == [2]
    plan = audit.grant_plan(_rows(), role="operator", phone=None,
                            username="ops", all_roleless=False)
    assert [r["id"] for r in plan] == [2]


def test_grant_plan_is_idempotent_for_a_role_already_held(audit):
    assert audit.grant_plan(_rows(), role="superadmin", phone=None, username="admin",
                            all_roleless=False) == []


def test_grant_plan_refuses_to_guess_without_a_target(audit):
    assert audit.grant_plan(_rows(), role="viewer", phone=None, username=None,
                            all_roleless=False) == []


def test_bulk_grant_skips_privileged_and_disabled(audit):
    plan = audit.grant_plan(_rows(), role="viewer", phone=None, username=None,
                            all_roleless=True)
    assert [r["id"] for r in plan] == [2]


def test_selftest_runs_without_a_database():
    out = subprocess.run([sys.executable, str(SCRIPT), "--selftest"],
                         capture_output=True, text=True, timeout=120,
                         env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-800:]
    assert "selftest ok" in out.stdout


# ── wiring: the script must be usable, not just present ─────────────────────
def test_script_is_tracked_and_exposed():
    assert SCRIPT.is_file()
    assert "!/scripts/rbac_write_audit.py" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "rbac-audit:" in makefile
    assert "scripts/rbac_write_audit.py --strict" in makefile


def test_installer_reports_role_coverage():
    """The installer runs the audit so a fresh install never ships a roleless owner."""
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "scripts/rbac_write_audit.py" in installer
    assert "have NO role" in installer


def test_startup_check_is_called_from_lifespan():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "_log_rbac_role_coverage()" in source
    assert source.index("_log_rbac_role_coverage") < source.index("await start_pollers()")


def test_script_refuses_to_run_without_database_url():
    out = subprocess.run([sys.executable, str(SCRIPT)],
                         capture_output=True, text=True, timeout=120,
                         env={"PATH": "/usr/bin:/bin", "DATABASE_URL": ""})
    assert out.returncode == 2
    assert "DATABASE_URL" in (out.stderr + out.stdout)


# ── startup report ──────────────────────────────────────────────────────────
def _coverage(main_module, monkeypatch, *, active: int, with_token: int,
              minimum: int, caplog):
    async def _fake_fetch_one(_sql, *_args, **_kwargs):
        return {"active": active, "with_token": with_token}

    monkeypatch.setattr(main_module, "fetch_one", _fake_fetch_one)
    monkeypatch.setattr(main_module.settings, "rbac_write_min_level", minimum, raising=False)
    with caplog.at_level(logging.INFO, logger="fazle.app"):
        asyncio.run(main_module._log_rbac_role_coverage())
    return caplog.text


def test_startup_errors_when_enforcement_would_lock_out_a_token(main_module, monkeypatch, caplog):
    text = _coverage(main_module, monkeypatch, active=2, with_token=2, minimum=10, caplog=caplog)
    assert "ERROR" in text or "hold a token with no role" in text
    assert "rbac_write_audit.py" in text          # the operator is told how to fix it


def test_startup_is_silent_when_enforcement_has_full_coverage(main_module, monkeypatch, caplog):
    text = _coverage(main_module, monkeypatch, active=0, with_token=0, minimum=10, caplog=caplog)
    assert "every active token holder has a role" in text
    assert "no role" not in text.split("every active token holder")[0].split()[-3:]


def test_startup_warns_in_audit_mode(main_module, monkeypatch, caplog):
    text = _coverage(main_module, monkeypatch, active=3, with_token=1, minimum=0, caplog=caplog)
    assert "3 active admin account(s) have no role" in text
    assert "make rbac-audit" in text


def test_startup_does_not_warn_when_everyone_has_a_role(main_module, monkeypatch, caplog):
    text = _coverage(main_module, monkeypatch, active=0, with_token=0, minimum=0, caplog=caplog)
    assert "have no role" not in text


# ── real SQL ────────────────────────────────────────────────────────────────
async def test_roleless_token_holder_is_found_in_the_live_schema(test_db_pool):
    audit = _load_script()
    async with test_db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fazle_admins (phone, name, username, status, api_key_hash) "
            "VALUES ('8801999000001', 'Roleless', 'roleless_it', 'active', 'x') "
            "ON CONFLICT (phone) DO UPDATE SET username=EXCLUDED.username, status='active'"
        )
        await conn.execute(
            "DELETE FROM fazle_admin_roles WHERE admin_id = "
            "(SELECT id FROM fazle_admins WHERE phone='8801999000001')"
        )
        rows, roles = await audit._load(conn)
    groups = audit.classify(rows)
    found = [r for r in groups["roleless_with_token"] if r["phone"] == "8801999000001"]
    assert found, "the SQL must report an active token holder with no role"
    assert found[0]["username"] == "roleless_it"
    assert roles, "fazle_roles must be seeded with the default ladder"


@pytest.mark.parametrize("role,level", [("viewer", 10), ("operator", 30),
                                        ("accountant", 50), ("admin", 70),
                                        ("superadmin", 100)])
async def test_default_role_ladder_matches_the_documented_levels(test_db_pool, role, level):
    async with test_db_pool.acquire() as conn:
        got = await conn.fetchval("SELECT level FROM fazle_roles WHERE name=$1", role)
    assert got == level
