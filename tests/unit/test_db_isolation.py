"""Tests for `tests/support/db_isolation.py` — the per-test database reset.

The regression these protect against is real and was measured: eleven tests
(`test_rbac`, `test_route_b_handoff`, `test_stage_a_live_escort_verification`,
`test_trusted_group_escort_orders`, `test_bridge_send_control_maintenance`, …)
passed on their own and failed in a full session, because tests committed rows
and the next test that inserted the same admin phone numbers died on

    duplicate key value violates unique constraint "fazle_admins_phone_key"

A suite that only passes in one order cannot gate a release.
"""
from __future__ import annotations

import os

import asyncpg
import pytest

from tests.support import db_isolation

# Same construction as tests/conftest.py: the password comes from the
# environment, so no credential-shaped literal lives in the file.
TEST_DB_URL = os.getenv("TEST_DATABASE_URL") or (
    f"postgresql://postgres:{os.getenv('TEST_DB_PASSWORD', 'changeme-set-TEST_DB_PASSWORD')}"
    "@127.0.0.1:5433/fazle_test"
)

pytestmark = pytest.mark.unit


async def _plant_admin(phone: str = "8801700000123", *, strict: bool = False) -> None:
    """Insert a probe admin row. `strict=True` lets a duplicate raise, which is
    how the original full-suite failure surfaced (`UniqueViolationError`)."""
    sql = "INSERT INTO fazle_admins (phone, name, status) VALUES ($1, 'isolation-probe', 'active')"
    if not strict:
        sql += " ON CONFLICT (phone) DO NOTHING"
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(sql, phone)
    finally:
        await conn.close()


async def _admin_exists(phone: str) -> bool:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM fazle_admins WHERE phone = $1)", phone
        )
    finally:
        await conn.close()


class TestRoleLadderSurvives:
    """The role catalog is reference data, not test data."""

    async def test_roles_are_kept_and_counted_as_nondirty(self):
        tables = await db_isolation.reset_database_async(TEST_DB_URL)
        assert "fazle_roles" not in tables
        assert "fazle_roles" in db_isolation.KEEP_TABLES

    async def test_reset_restores_the_full_role_ladder(self):
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            await conn.execute("DELETE FROM fazle_roles")
        finally:
            await conn.close()

        await db_isolation.reset_database_async(TEST_DB_URL)

        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            rows = await conn.fetch("SELECT name, level FROM fazle_roles")
            levels = {r["name"]: r["level"] for r in rows}
        finally:
            await conn.close()

        assert levels["viewer"] == 10
        assert levels["admin"] == 70
        assert levels["superadmin"] == 100


class TestResetEmptiesData:
    async def test_planted_row_is_removed(self):
        await _plant_admin("8801700000124")
        assert await _admin_exists("8801700000124")

        truncated = await db_isolation.reset_database_async(TEST_DB_URL)

        assert "fazle_admins" in truncated
        assert not await _admin_exists("8801700000124")

    async def test_clean_database_costs_nothing_to_reset(self):
        """A clean schema must not pay the 2.4 s full-schema TRUNCATE price."""
        await db_isolation.reset_database_async(TEST_DB_URL)
        again = await db_isolation.reset_database_async(TEST_DB_URL)
        assert again == []

    async def test_only_dirty_tables_are_truncated(self):
        """The reset must not touch 130 relations to clear one row."""
        await db_isolation.reset_database_async(TEST_DB_URL)
        await _plant_admin("8801700000125")

        truncated = await db_isolation.reset_database_async(TEST_DB_URL)

        assert truncated == ["fazle_admins"]

    async def test_a_second_insert_of_the_same_admin_no_longer_collides(self):
        """The exact failure mode from the full-suite run, in miniature."""
        await _plant_admin("8801700000126", strict=True)
        await db_isolation.reset_database_async(TEST_DB_URL)

        # Before isolation existed this raised asyncpg.UniqueViolationError.
        await _plant_admin("8801700000126", strict=True)
        assert await _admin_exists("8801700000126")


class TestReporting:
    def test_keep_tables_is_documented_catalog_data(self):
        assert db_isolation.KEEP_TABLES == frozenset({"fazle_roles"})

    def test_describe_truncates_long_lists(self):
        text = db_isolation.describe(["a", "b", "c", "d", "e", "f"], limit=4)
        assert text.startswith("a, b, c, d")
        assert "+2" in text

    def test_reset_without_a_url_is_an_error_not_a_silent_noop(self):
        with pytest.raises(db_isolation.DatabaseIsolationError):
            db_isolation.reset_database("")

    def test_marker_is_registered_so_opt_out_works(self):
        """`--strict-markers` would reject an unregistered `no_db_isolation`."""
        import pathlib

        for ini in ("pytest.ini", "tests/pytest.ini"):
            text = (pathlib.Path(__file__).resolve().parents[2] / ini).read_text(
                encoding="utf-8"
            )
            assert "no_db_isolation:" in text, ini
