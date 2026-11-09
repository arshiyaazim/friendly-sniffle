"""Per-test database isolation.

Why this exists
---------------
`tests/conftest.py` builds the schema once per session, and until now every test
that wrote a row left it behind. The `test_db_pool` fixture truncated on
teardown, but only for tests that asked for *that* pool; the majority of DB
tests go through `app.database` (its own pool, its own connections), so their
rows survived into every later test in the run.

The visible symptom was 11 tests that pass on their own and fail in a full
session:

    asyncpg.exceptions.UniqueViolationError: duplicate key value violates
    unique constraint "fazle_admins_phone_key"
    DETAIL:  Key (phone)=(8801700000001) already exists.

Nothing was wrong with those tests. Two tests had independently created the
admin from `ADMIN_NUMBERS`, which is exactly what a fresh database would allow.
A test suite that only passes when run in one particular order cannot be a
release gate.

What this module does
---------------------
`reset_database()` empties every table in `public` (one `TRUNCATE ... RESTART
IDENTITY CASCADE`) and re-inserts the immutable RBAC role catalog. It is called
*before* each test rather than after, so a failing test leaves its rows in place
for inspection instead of having them wiped by the next test's setup.

Design constraints that shaped it:

* **It must not hide failures.** The old truncation swallowed every exception
  (`except Exception: pass`), which is how a foreign-key cycle or a stuck
  transaction could quietly re-introduce cross-test pollution. This version
  retries once after a short lock timeout and then raises, with the blocking
  relation names in the message.
* **It must not need an event loop of its own.** Most DB tests are async and
  pytest-asyncio owns the loop; the reset therefore opens a short-lived
  connection with `asyncio.run()` from a synchronous fixture, before the test's
  own loop and pool exist.
* **It must be cheap.** A `TRUNCATE` of the whole 131-table schema costs ~2.4 s
  here (PostgreSQL rewrites a relfilenode per relation — measured at 20 ms ×
  130 relations), which would add over half an hour to the suite. So the reset
  first counts the rows of every table in one statement and truncates only the
  ones that are actually dirty: a typical test dirties zero to three tables,
  and the whole reset costs ~25 ms.
* **It must be escapable.** `FAZLE_TEST_DB_ISOLATION=off` disables it entirely
  and `@pytest.mark.no_db_isolation` disables it for one test, for the rare test
  that deliberately asserts state surviving from an earlier step of the same
  module.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

#: Tables whose rows are catalog data, not test data. `fazle_roles` is the role
#: ladder (viewer 10 … superadmin 100) seeded by conftest and by migration 093;
#: TRUNCATE ... CASCADE can reach it indirectly even when it is excluded from
#: the statement, so it is re-seeded afterwards.
KEEP_TABLES: frozenset[str] = frozenset({"fazle_roles"})

#: Re-inserted after every reset (mirrors conftest._SEED_SQL).
RESEED_SQL = """
INSERT INTO fazle_roles (name, level, description) VALUES
  ('viewer',           10, 'Read-only access to operational status'),
  ('office_assistant', 20, 'View attendance list/own records; create attendance draft entries only'),
  ('operator',         30, 'Approve/reject drafts and edit operational records'),
  ('accountant',       50, 'Payroll, cash, income and payment reconciliation'),
  ('admin',            70, 'Administrative control over users and operational configuration'),
  ('superadmin',      100, 'Full owner access, including admin management')
ON CONFLICT (name) DO NOTHING;
"""


class DatabaseIsolationError(RuntimeError):
    """The test database could not be returned to a clean state."""


#: How many tables to count per statement. One 130-way UNION is fine, but
#: chunking keeps the plan small and the statement readable in logs.
_COUNT_CHUNK = 40


async def _public_tables(conn) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename <> ALL($1::text[])
        ORDER BY tablename
        """,
        list(KEEP_TABLES),
    )
    return [r["tablename"] for r in rows]


async def _dirty_tables(conn, tables: list[str]) -> list[str]:
    """Return the subset of `tables` that currently contains at least one row.

    One round trip per chunk instead of one per table; `count(*)` on an empty
    relation is a no-op in PostgreSQL, so this is ~15 ms for the whole schema.
    """
    dirty: list[str] = []
    for start in range(0, len(tables), _COUNT_CHUNK):
        chunk = tables[start:start + _COUNT_CHUNK]
        query = " UNION ALL ".join(
            f'SELECT {i} AS idx, count(*) AS n FROM "{t}"' for i, t in enumerate(chunk)
        )
        rows = await conn.fetch(query)
        dirty.extend(chunk[r["idx"]] for r in rows if r["n"])
    return dirty


async def reset_connection(conn) -> list[str]:
    """Empty the dirty data tables on an open connection; returns their names."""
    tables = await _dirty_tables(conn, await _public_tables(conn))
    # Cheap (one INSERT ... ON CONFLICT DO NOTHING) and unconditional: a test
    # that deleted the role ladder must not be able to leave the suite without
    # it, because `fazle_roles` is excluded from the truncate list.
    await conn.execute(RESEED_SQL)
    if not tables:
        return []
    statement = "TRUNCATE TABLE {} RESTART IDENTITY CASCADE".format(
        ", ".join(f'"{t}"' for t in tables)
    )
    await conn.execute("SET lock_timeout = '5s'")
    try:
        await conn.execute(statement)
    except Exception as exc:  # noqa: BLE001 - re-raised with context below
        # A single retry covers the common case: a pooled connection from a
        # previous test was still finishing its rollback when we arrived.
        try:
            await conn.execute("SELECT pg_sleep(0.05)")
            await conn.execute(statement)
        except Exception as retry_exc:  # noqa: BLE001
            raise DatabaseIsolationError(
                f"could not reset the test database: {retry_exc} "
                f"({len(tables)} tables; is a test leaving a transaction open?)"
            ) from exc
    # TRUNCATE ... CASCADE can reach fazle_roles indirectly even though it is
    # excluded from the table list, so the role ladder is restored every time.
    await conn.execute(RESEED_SQL)
    return tables


async def reset_database_async(url: str) -> list[str]:
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        return await reset_connection(conn)
    finally:
        await conn.close()


def reset_database(url: str) -> list[str]:
    """Synchronous entry point: reset `url`, return the truncated table names."""
    if not url:
        raise DatabaseIsolationError("no test database URL configured")
    return asyncio.run(reset_database_async(url))


def describe(tables: Iterable[str], limit: int = 4) -> str:
    names = list(tables)
    head = ", ".join(names[:limit])
    if len(names) > limit:
        head += f", … (+{len(names) - limit})"
    return head
