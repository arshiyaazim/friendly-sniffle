"""Deploy-artifact regressions: the systemd unit and its installer.

Two things broke before and are guarded here:

1. ``install.sh`` installed ``fazle-core.service`` from the repository root,
   but that file was never committed — the VPS install failed at the
   ``sudo cp`` step on every clean checkout.
2. The unit's ``StandardError=`` target and the restart gate's ``--err-log``
   default have to be the same file. If they drift, the gate can never see
   ``Application startup complete.`` and reports "no startup signal" on a
   perfectly healthy deploy.

Pure unit tests: read the files, no systemd, no installation.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from scripts import restart_gate_readiness as gate

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
UNIT_TEMPLATE = REPO_ROOT / "fazle-core.service"
INSTALLER = REPO_ROOT / "install.sh"

KNOWN_PLACEHOLDERS = {"@APP_DIR@", "@RUN_USER@"}
_PLACEHOLDER_RE = re.compile(r"@[A-Z_]+@")


def _directives(text: str) -> list[str]:
    """Unit lines that carry meaning (not comments, not section headers)."""
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith(("#", "[", ";"))]


def _render(text: str) -> str:
    return (text.replace("@APP_DIR@", "/srv/fazle-core")
                .replace("@RUN_USER@", "fazle"))


class TestUnitTemplate:
    def test_template_and_installer_both_exist(self):
        assert UNIT_TEMPLATE.is_file(), "fazle-core.service template is missing"
        assert INSTALLER.is_file()

    def test_installer_renders_the_unit_instead_of_copying_it(self):
        installer = INSTALLER.read_text()
        assert "fazle-core.service" in installer or "$UNIT_NAME.service" in installer
        assert "sed" in installer, "installer must substitute the placeholders"
        assert "install -m 644" in installer

    def test_every_placeholder_is_one_the_installer_substitutes(self):
        used = set(_PLACEHOLDER_RE.findall("\n".join(_directives(UNIT_TEMPLATE.read_text()))))
        assert used == KNOWN_PLACEHOLDERS, f"unknown placeholders: {used - KNOWN_PLACEHOLDERS}"

    def test_rendered_unit_has_no_placeholders_left(self):
        rendered = _render(UNIT_TEMPLATE.read_text())
        leftovers = _PLACEHOLDER_RE.findall("\n".join(_directives(rendered)))
        assert leftovers == [], f"unsubstituted: {leftovers}"

    def test_runs_the_checkout_with_its_own_venv(self):
        rendered = _render(UNIT_TEMPLATE.read_text())
        exec_start = next(d for d in _directives(rendered) if d.startswith("ExecStart="))
        assert "/srv/fazle-core/venv/bin/python" in exec_start
        assert exec_start.endswith("/srv/fazle-core/run.py")

    def test_working_directory_and_env_file_point_at_the_checkout(self):
        directives = _directives(_render(UNIT_TEMPLATE.read_text()))
        assert "WorkingDirectory=/srv/fazle-core" in directives
        assert "EnvironmentFile=-/srv/fazle-core/.env" in directives

    def test_restart_policy_is_bounded_not_aggressive(self):
        directives = _directives(_render(UNIT_TEMPLATE.read_text()))
        assert "Restart=on-failure" in directives
        assert any(d.startswith("RestartSec=") for d in directives)
        assert any(d.startswith("TimeoutStopSec=") for d in directives)

    def test_install_target_is_present(self):
        assert "[Install]" in UNIT_TEMPLATE.read_text()
        assert "WantedBy=multi-user.target" in _directives(UNIT_TEMPLATE.read_text())


class TestGateAndUnitAgree:
    def test_stderr_log_is_the_file_the_gate_scans(self):
        rendered = _render(UNIT_TEMPLATE.read_text())
        stderr_line = next(d for d in _directives(rendered) if d.startswith("StandardError="))
        unit_log = pathlib.Path(stderr_line.split(":", 1)[1])
        assert unit_log.name == pathlib.Path(gate.DEFAULT_ERR_LOG).name, (
            "fazle-core.service writes stderr to a different file than "
            "restart_gate_readiness.DEFAULT_ERR_LOG reads")
        assert unit_log.parent.name == "logs"

    def test_unit_name_matches_the_gate_default(self):
        assert gate.DEFAULT_SERVICE == "fazle-core"

    def test_installer_verifies_readiness_with_the_gate(self):
        installer = INSTALLER.read_text()
        assert "restart_gate_readiness.py" in installer
        assert "--restarts-before" in installer, (
            "without the pre-restart counter the gate cannot detect crash loops")
        assert "--err-log" in installer
