#!/usr/bin/env python3
"""Migration runner.

Usage:
    python db/migrate.py --dry-run              # inventory only, no database needed
    DATABASE_URL=... python db/migrate.py --dry-run   # list what is actually pending
    DATABASE_URL=... python db/migrate.py       # apply pending migrations

`--dry-run` without DATABASE_URL prints the local migration inventory and exits
without touching a database. With DATABASE_URL set it compares against
schema_migrations and lists the genuinely pending files — still without
creating tables or applying anything.
"""
import asyncio
import glob
import os
import sys

import asyncpg

DB_URL = os.environ.get("DATABASE_URL")
MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")
DRY_RUN = "--dry-run" in sys.argv


def _migration_files():
    return sorted(glob.glob(f"{MIGRATIONS_DIR}/*.sql"))


def _report_offline(migration_files):
    """No DATABASE_URL: we cannot know what is applied, so say exactly that."""
    print("DATABASE_URL is not set — listing the local migration inventory only.")
    print("Which of these are already applied can only be answered against a database.")
    print()
    for path in migration_files:
        print(f"  file    {os.path.basename(path)}")
    print()
    print(f"{len(migration_files)} migration file(s) in {MIGRATIONS_DIR}")
    print("Re-run with DATABASE_URL=... --dry-run to see what is actually pending.")


async def run():
    migration_files = _migration_files()
    if not migration_files:
        print("No migration files found in", MIGRATIONS_DIR)
        return

    if not DB_URL:
        if DRY_RUN:
            _report_offline(migration_files)
            return
        print("ERROR: DATABASE_URL is required to apply migrations.", file=sys.stderr)
        print("       Use --dry-run for a database-free inventory.", file=sys.stderr)
        sys.exit(2)

    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            if DRY_RUN:
                # Read-only: never create schema_migrations just to inspect it.
                has_table = await conn.fetchval(
                    "SELECT to_regclass('public.schema_migrations') IS NOT NULL"
                )
                applied = ({r["name"] for r in
                            await conn.fetch("SELECT name FROM schema_migrations")}
                           if has_table else set())
            else:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        name       TEXT        PRIMARY KEY,
                        applied_at TIMESTAMPTZ DEFAULT NOW()
                    )""")
                applied = {r["name"] for r in
                           await conn.fetch("SELECT name FROM schema_migrations")}

            for path in migration_files:
                name = os.path.basename(path)
                if name in applied:
                    print(f"  skip    {name}")
                    continue
                if DRY_RUN:
                    print(f"  pending {name}  [dry-run, not applied]")
                    continue
                sql = open(path, encoding="utf-8").read()
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations(name) VALUES($1)", name)
                print(f"  applied {name}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
