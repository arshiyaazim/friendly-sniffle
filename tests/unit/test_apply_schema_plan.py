"""scripts/apply_schema.py — the schema build plan itself.

The plan is the load-bearing part: `db/migrations/` alone cannot build a virgin
database, because it asserts tables created by two older standalone sets. These
tests pin the order (payroll → escort → db/migrations) and the naming contract
that keeps `scripts/apply_schema.py` and `db/migrate.py` agreeing about what has
already been applied.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "apply_schema.py"


def _load(argv=()):
    """Import the script with a controlled sys.argv (it parses flags at import)."""
    old_argv = sys.argv[:]
    sys.argv = ["apply_schema.py", *argv]
    try:
        spec = importlib.util.spec_from_file_location("apply_schema_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = old_argv


def test_plan_has_the_three_sets_in_order():
    plan = _load()._plan()
    groups = [g for _key, _path, g in plan]
    # every payroll file comes before every escort file, which comes before core
    assert groups == sorted(groups, key=["payroll", "escort", "core"].index)
    assert groups.count("payroll") == 14
    assert groups.count("escort") == 2
    assert groups.count("core") == 92
    assert len(plan) == 108


def test_core_keys_match_db_migrate_py_names():
    """db/migrate.py records bare basenames; apply_schema must agree, or the two
    runners would apply the same file twice."""
    plan = _load()._plan()
    core = [key for key, _path, group in plan if group == "core"]
    assert core == sorted(core)
    assert all("/" not in key for key in core)
    assert "092_admin_bootstrap_credentials.sql" in core
    assert "093_seed_rbac_roles.sql" in core


def test_prerequisite_keys_are_path_scoped():
    plan = _load()._plan()
    keys = [key for key, _path, group in plan if group in ("payroll", "escort")]
    assert all(key.startswith(("modules/fazle_payroll_engine/migrations/", "migrations/"))
               for key in keys)
    assert "modules/fazle_payroll_engine/migrations/001_fpe_schema.sql" in keys
    assert "migrations/escort_history_schema.sql" in keys
    assert "migrations/escort_roster_schema.sql" in keys


def test_every_planned_file_exists_and_is_non_empty():
    for _key, path, _group in _load()._plan():
        assert Path(path).is_file(), path
        assert Path(path).stat().st_size > 0, path


def test_prereq_tables_really_are_absent_from_core_set():
    """Regression guard for the finding this script exists for: 030/046 depend on
    fpe_employees, 024 on escort_order_groups, and neither table is created by
    db/migrations."""
    core_sql = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted((ROOT / "db" / "migrations").glob("*.sql"))
    ).lower()
    # the dependencies are referenced by db/migrations but never created there
    assert "references fpe_employees" in core_sql or "on fpe_employees" in core_sql
    assert "create table if not exists fpe_employees" not in core_sql
    assert "create table if not exists escort_order_groups" not in core_sql
    assert "references escort_order_groups" in core_sql

    prereq_sql = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted((ROOT / "modules" / "fazle_payroll_engine" / "migrations").glob("*.sql"))
    ).lower()
    assert "create table if not exists fpe_employees" in prereq_sql

    escort = (ROOT / "migrations" / "escort_history_schema.sql").read_text(
        encoding="utf-8", errors="ignore").lower()
    assert "create table if not exists escort_order_groups" in escort


def test_only_db_scope_excludes_prerequisites():
    plan = _load(["--only", "db"])._plan()
    assert {group for _k, _p, group in plan} == {"core"}
    assert len(plan) == 92


def test_known_gap_list_matches_the_real_blockers():
    """The gap list is documentation with a test: it must name the objects that
    actually stop a virgin build (verified against PostgreSQL 16)."""
    mod = _load()
    expected = {
        "wbom_candidates",
        "wbom_candidate_conversations",
        "fazle_knowledge_base.value",
        "fazle_knowledge_base.subcategory",
        "fazle_knowledge_base.status",
        "fazle_payment_drafts.employee_phone",
        "role fazle_ai_reader",
    }
    assert set(mod.KNOWN_MISSING_OBJECTS) == expected
    assert set(mod.VIRGIN_NOOP_FILES) == {"086_k32_recruitment_reconciliation.sql"}
    assert set(mod.HISTORICAL_ASSUMPTIONS) == {
        "003_kb_schema_fix_and_recruitment.sql",
        "003c_schema_align.sql",
    }


def test_installer_uses_the_full_builder():
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "scripts/apply_schema.py" in installer
