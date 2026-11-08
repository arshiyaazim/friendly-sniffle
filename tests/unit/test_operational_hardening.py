"""Operational hardening invariants (2026-11-06 audit).

These are cheap structural tests for things that silently regress and then only
show up on the deployment host: the systemd unit losing its umask/crash-loop
settings, the installer dropping its pre-migration backup, the log file becoming
world-readable again, or a security warning disappearing from startup.

Each assertion points at the README section that explains *why* the setting
exists — see "Operational readiness audit" in README.md.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

UNIT = (ROOT / "fazle-core.service").read_text(encoding="utf-8")
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
RUN_PY = (ROOT / "run.py").read_text(encoding="utf-8")


# ── systemd ──────────────────────────────────────────────────────────────────
def test_unit_runs_as_rendered_user_not_root():
    assert "User=@RUN_USER@" in UNIT
    assert "Group=@RUN_USER@" in UNIT
    assert "WorkingDirectory=@APP_DIR@" in UNIT


def test_unit_has_restart_and_crash_loop_policy():
    assert "Restart=on-failure" in UNIT
    assert re.search(r"^RestartSec=\d+", UNIT, re.M)
    assert re.search(r"^StartLimitIntervalSec=\d+", UNIT, re.M)
    assert re.search(r"^StartLimitBurst=\d+", UNIT, re.M)


def test_unit_environment_file_is_optional():
    """.env must never be the reason a service refuses to start."""
    assert "EnvironmentFile=-@APP_DIR@/.env" in UNIT


def test_unit_umask_keeps_new_files_private():
    assert "UMask=0077" in UNIT


def test_unit_hardening_flags():
    assert "NoNewPrivileges=true" in UNIT
    assert "PrivateTmp=true" in UNIT


# ── network binding ──────────────────────────────────────────────────────────
def test_run_py_binds_loopback_only():
    assert "127.0.0.1" in RUN_PY
    assert "0.0.0.0" not in RUN_PY


# ── installer ────────────────────────────────────────────────────────────────
def test_installer_takes_a_pre_migration_backup():
    assert "pg_dump" in INSTALL
    assert "pre-migrate-" in INSTALL
    assert "SKIP_PRE_MIGRATION_BACKUP" in INSTALL
    # the rollback hint must not print the database password
    assert "1:***@#" in INSTALL


def test_installer_checks_the_port_before_touching_anything():
    assert "ss -ltnp" in INSTALL or "lsof" in INSTALL
    assert "already in use" in INSTALL


def test_installer_refuses_a_half_built_schema():
    assert "ALLOW_INCOMPLETE_SCHEMA" in INSTALL
    assert 'SCHEMA_RC" -eq 3' in INSTALL


def test_installer_keeps_env_and_backups_private():
    assert "chmod 600" in INSTALL          # .env + pre-migration dump
    assert "chmod 700" in INSTALL          # backups directory


def test_schema_builder_is_tracked_and_guarded():
    """The merged tree shipped an installer whose schema builder was gitignored."""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!/scripts/apply_schema.py" in ignore
    assert (ROOT / "scripts" / "apply_schema.py").is_file()
    assert "the checkout is incomplete" in INSTALL


def test_installer_avoids_cuda_wheels():
    assert "download.pytorch.org/whl/cpu" in INSTALL
    assert "NO_CPU_TORCH" in INSTALL


# ── runtime logging / warnings ───────────────────────────────────────────────
def test_log_file_is_private():
    src = (ROOT / "app" / "logging_setup.py").read_text(encoding="utf-8")
    assert "os.chmod(log_path, 0o600)" in src


def test_startup_warns_about_unsigned_bridge_webhooks():
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "WEBHOOK_SIGNATURE_ENFORCEMENT" in src
    assert "accept UNSIGNED" in src


def test_startup_warns_about_a_weak_internal_api_key():
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "[security] INTERNAL_API_KEY looks weak" in src


# ── documentation stays attached to the audit ────────────────────────────────
def test_readme_documents_every_audited_area():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Operational readiness audit" in readme
    for heading in (
        "### Git", "### systemd", "### File permissions", "### Network",
        "### Database", "### Migration", "### API", "### RBAC",
        "### Background jobs and duplicate workers", "### WhatsApp access",
        "### Resource usage", "### Recovery", "### Test suite reality",
        "### Known flaky test",
    ):
        assert heading in readme, heading


def test_flaky_concurrency_test_stubs_all_four_probes():
    """The failure mode documented in the README: a timing assertion that
    measured the network because `_check_groq` stayed real."""
    src = (ROOT / "tests" / "unit" / "test_llm_provider_order.py").read_text(encoding="utf-8")
    block = src.split("test_all_four_checks_actually_run_concurrently", 1)[1]
    block = block.split("def test_return_shape_unchanged", 1)[0]
    assert 'monkeypatch.setattr(llm, "_check_groq"' in block
    assert 'monkeypatch.setattr(llm, "_check_omniroute"' in block
    assert 'monkeypatch.setattr(llm, "_check_github_models"' in block
    assert 'monkeypatch.setattr(llm, "_check_ollama"' in block
