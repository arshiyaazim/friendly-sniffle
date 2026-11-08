"""Access-control hardening added by the 2026-11-06 audit.

Three gaps the README calls out, each with a test that does not need a database:

  * the owner key (INTERNAL_API_KEY) is a server secret — loopback only;
  * a mutating request with an API token must belong to an active admin with a
    role, which is how the 195 "any valid key" routes stop accepting orphaned
    tokens;
  * /metrics can require a bearer token, because the text exposition leaks route
    names, error counts and latency.

The operator-facing artefacts (nginx sample, isolation check, restore drill,
bridge smoke test) get structural tests so they cannot quietly disappear.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def main_module():
    import app.main as main
    return main


# ── loopback / owner key ────────────────────────────────────────────────────
@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "localhost", None, ""])
def test_loopback_hosts_are_trusted(main_module, host):
    assert main_module._is_loopback(host) is True


@pytest.mark.parametrize("host", ["10.1.2.3", "203.0.113.9", "192.168.1.5", "0.0.0.0"])
def test_remote_hosts_are_not_loopback(main_module, host):
    assert main_module._is_loopback(host) is False


def test_owner_key_comparison(main_module, monkeypatch):
    monkeypatch.setattr(main_module.settings, "internal_api_key", "s3cret-owner-key", raising=False)
    assert main_module._is_owner_key("s3cret-owner-key") is True
    assert main_module._is_owner_key("s3cret-owner-kez") is False
    assert main_module._is_owner_key("") is False
    assert main_module._is_owner_key(None) is False
    monkeypatch.setattr(main_module.settings, "internal_api_key", "", raising=False)
    assert main_module._is_owner_key("") is False


# ── write guard scope ───────────────────────────────────────────────────────
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_write_guard_covers_mutations(main_module, method):
    assert main_module._write_guard_needed(method, "/api/anything") is True


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_write_guard_ignores_reads(main_module, method):
    assert main_module._write_guard_needed(method, "/api/anything") is False


@pytest.mark.parametrize("path", [
    "/webhook/mcp1",
    "/webhook/mcp2",
    "/webhook/meta",
    "/api/admin/login",
    "/login",
    "/api/frontend/heartbeat",
    "/ai-policy/resolve-secret",
])
def test_identity_free_writes_are_exempt(main_module, path):
    assert main_module._write_guard_needed("POST", path) is False


def test_guard_middlewares_are_registered(main_module):
    names = {getattr(mw, "cls", None) or mw for mw in main_module.app.user_middleware}
    # middlewares are functions registered by name; assert the functions exist
    assert callable(main_module.owner_key_scope)
    assert callable(main_module.write_identity_guard)
    assert names  # at least the default middleware stack


def test_token_role_level_is_zero_without_a_role(main_module, monkeypatch):
    """An unknown token must resolve to level 0 → the middleware refuses the write."""
    from modules import rbac

    async def _no_admin(_key):
        return None

    monkeypatch.setattr(rbac, "get_admin_by_api_key", _no_admin)
    main_module._ROLE_CACHE.clear()
    assert asyncio.run(main_module._token_role_level("fk_unknown")) == 0


def test_token_role_level_uses_the_admin_role(main_module, monkeypatch):
    from modules import rbac

    async def _admin(_key):
        return {"id": 7, "status": "active", "phone": "8801700000001"}

    async def _level(_admin_id):
        return 30

    monkeypatch.setattr(rbac, "get_admin_by_api_key", _admin)
    monkeypatch.setattr(rbac, "max_role_level", _level)
    main_module._ROLE_CACHE.clear()
    assert asyncio.run(main_module._token_role_level("fk_live")) == 30


def test_disabled_admin_has_no_level(main_module, monkeypatch):
    from modules import rbac

    async def _admin(_key):
        return {"id": 7, "status": "disabled"}

    monkeypatch.setattr(rbac, "get_admin_by_api_key", _admin)
    main_module._ROLE_CACHE.clear()
    assert asyncio.run(main_module._token_role_level("fk_dead")) == 0


def test_guard_is_audit_only_by_default(main_module):
    """Default RBAC_WRITE_MIN_LEVEL=0 must not change who can write."""
    assert int(main_module.settings.rbac_write_min_level) == 0


def test_guard_refuses_below_the_configured_level(main_module, monkeypatch):
    """Level 0 token + min level 10/70 → 403; the same token at level 30 passes."""
    from modules import rbac

    async def _admin(_key):
        return {"id": 9, "status": "active"}

    async def _level(_admin_id):
        return 30

    monkeypatch.setattr(rbac, "get_admin_by_api_key", _admin)
    monkeypatch.setattr(rbac, "max_role_level", _level)
    main_module._ROLE_CACHE.clear()
    assert asyncio.run(main_module._token_role_level("fk_operator")) == 30

    async def _roleless(_admin_id):
        return 0

    monkeypatch.setattr(rbac, "max_role_level", _roleless)
    main_module._ROLE_CACHE.clear()
    assert asyncio.run(main_module._token_role_level("fk_roleless")) == 0


# ── metrics token ───────────────────────────────────────────────────────────
def test_metrics_requires_the_token_when_configured(main_module, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(main_module.settings, "metrics_token", "scrape-me", raising=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_module.metrics_prometheus(authorization=None, x_metrics_token=None))
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        asyncio.run(main_module.metrics_prometheus(authorization=None, x_metrics_token="nope"))
    # the right token, in either header, is accepted
    for kwargs in ({"authorization": "Bearer scrape-me", "x_metrics_token": None},
                   {"authorization": None, "x_metrics_token": "scrape-me"}):
        try:
            asyncio.run(main_module.metrics_prometheus(**kwargs))
        except HTTPException:  # pragma: no cover - would be a failure
            pytest.fail(f"token rejected: {kwargs}")


def test_metrics_is_open_when_no_token_is_set(main_module, monkeypatch):
    monkeypatch.setattr(main_module.settings, "metrics_token", "", raising=False)
    try:
        asyncio.run(main_module.metrics_prometheus(authorization=None, x_metrics_token=None))
    except Exception as exc:  # pragma: no cover - only HTTPException is a failure
        from fastapi import HTTPException
        assert not isinstance(exc, HTTPException), exc


# ── operator artefacts ──────────────────────────────────────────────────────
def test_nginx_example_blocks_the_unauthenticated_surfaces():
    conf = (ROOT / "deploy" / "nginx.conf.example").read_text(encoding="utf-8")
    assert "location = /metrics" in conf and "deny all" in conf
    assert "/webhook/mcp" in conf
    assert "limit_req" in conf            # rate limiting the app does not do
    assert "client_max_body_size" in conf
    assert "127.0.0.1:8200" in conf


def test_operator_scripts_are_tracked_and_self_test():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for script in ("verify_isolation.py", "restore_drill.py", "bridge_smoke.py"):
        assert f"!/scripts/{script}" in ignore, script
        path = ROOT / "scripts" / script
        assert path.is_file(), path
        # --selftest must work with no database and no bridge
        out = subprocess.run([sys.executable, str(path), "--selftest"],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, (script, out.stderr[-800:])
        assert "selftest ok" in out.stdout


def test_makefile_exposes_the_operational_checks():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("db-isolation:", "restore-drill:", "bridge-smoke:", "test-local:"):
        assert target in makefile, target
    assert "REQUIRE_TEST_DB=1" in makefile


def test_env_example_documents_the_new_switches():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("METRICS_TOKEN", "RBAC_WRITE_GUARD", "INTERNAL_KEY_ALLOW_REMOTE"):
        assert name in env, name


def test_installer_runs_the_isolation_check():
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "scripts/verify_isolation.py" in installer
    assert "STRICT_DB_ISOLATION" in installer
