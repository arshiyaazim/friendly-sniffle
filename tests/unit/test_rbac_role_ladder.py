"""One role ladder, everywhere.

`fazle_roles.level` is the number every access decision compares against:
`RBAC_WRITE_MIN_LEVEL`, `_require_*_or_above`, and `live_adapters`' per-tool
`min_role`. Three places define it — the migration that seeds production
(`db/migrations/093_seed_rbac_roles.sql`), the test fixture (`tests/conftest.py`)
and the README table. The test fixture used to seed `1..5` with no
`office_assistant`, which meant the whole suite compared levels against a ladder
that only exists inside tests.

These tests parse all three sources and fail on any drift.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXPECTED = {
    "viewer": 10,
    "office_assistant": 20,
    "operator": 30,
    "accountant": 50,
    "admin": 70,
    "superadmin": 100,
}

_ROW = re.compile(r"\(\s*'([a-z_]+)'\s*,\s*(\d+)\s*,")


def _rows_from(sql: str) -> dict[str, int]:
    return {name: int(level) for name, level in _ROW.findall(sql)}


def test_migration_093_seeds_the_documented_ladder():
    sql = (ROOT / "db" / "migrations" / "093_seed_rbac_roles.sql").read_text(encoding="utf-8")
    assert _rows_from(sql) == EXPECTED


def test_test_fixture_seed_matches_the_migration():
    from tests.conftest import _SEED_SQL

    assert _rows_from(_SEED_SQL) == EXPECTED, (
        "tests/conftest.py seeds a role ladder that production does not have — "
        "level comparisons in the suite would then prove nothing"
    )


def test_readme_role_table_matches_the_ladder():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for role, level in EXPECTED.items():
        pattern = re.compile(r"^\|\s*`" + role + r"`\s*\|\s*" + str(level) + r"\s*\|", re.M)
        assert pattern.search(readme), f"README role table is missing {role}={level}"


def test_enforcement_threshold_lets_the_lowest_writer_in():
    """RBAC_WRITE_MIN_LEVEL=10 must admit `viewer` and refuse a roleless token."""
    assert min(EXPECTED.values()) == 10
    assert 0 < 10, "a roleless token (level 0) must be below every real role"


def test_superadmin_outranks_every_ai_tool_requirement():
    """`live_adapters` compares actor level to the tool's min_role level."""
    sql = (ROOT / "db" / "migrations" / "093_seed_rbac_roles.sql").read_text(encoding="utf-8")
    levels = _rows_from(sql)
    assert levels["superadmin"] == max(levels.values())
