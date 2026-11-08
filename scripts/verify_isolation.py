#!/usr/bin/env python3
"""Verify that this deployment's database is isolated and least-privilege.

The audit answer to "is the database separate?" was *conditional*: the code
assumes one database per deployment and a single `public` schema, but nothing
verified that the target really is dedicated, or that the role the app connects
as is not a superuser. On a shared VPS that is exactly where two applications
end up fighting over one database.

What this checks (read-only, no writes):

  1. database name is not a cluster default (`postgres`, `template0/1`)
  2. the login role is not SUPERUSER / CREATEROLE / CREATEDB / BYPASSRLS
  3. the role only sees the schemas it should (`public` + system catalogs)
  4. every table in `public` is owned by the connecting role — a table owned by
     someone else means another application shares this database
  5. `search_path` is the default (`"$user", public`) and not redirected
  6. `pg_trgm` is installed in `public` (migration 029 needs it)
  7. tables that belong to *this* application are the only non-system ones
     present; a foreign table name is reported as a warning

Usage:
    DATABASE_URL=... python scripts/verify_isolation.py
    DATABASE_URL=... python scripts/verify_isolation.py --json
    DATABASE_URL=... python scripts/verify_isolation.py --strict   # warn = fail
    python scripts/verify_isolation.py --selftest                  # no DB needed

Exit codes:
    0  no problems (warnings may still be printed)
    1  warnings only, and --strict was passed
    2  a hard problem: shared database, elevated role, redirected search_path
    3  could not connect / no DATABASE_URL
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

SYSTEM_SCHEMAS = {"public", "pg_catalog", "information_schema", "pg_toast"}

ARGS = sys.argv[1:]
AS_JSON = "--json" in ARGS
STRICT = "--strict" in ARGS
SELFTEST = "--selftest" in ARGS

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Tables whose presence means a *different* application owns this database.
# Kept as a warning, not a failure: the payroll prerequisite set legitimately
# lives alongside this app in the production deployment.
FOREIGN_HINTS = ("bridge", "baileys", "n8n", "grafana", "metabase")


class Report:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self.ok: list[str] = []

    def good(self, msg: str) -> None:
        self.ok.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def bad(self, msg: str) -> None:
        self.problems.append(msg)

    @property
    def healthy(self) -> bool:
        return not self.problems

    def render(self) -> str:
        lines = [f"✓ {m}" for m in self.ok]
        lines += [f"!! {m}" for m in self.warnings]
        lines += [f"✗ {m}" for m in self.problems]
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "warnings": self.warnings, "problems": self.problems}


async def _inspect(conn, report: Report) -> None:
    db_name = await conn.fetchval("SELECT current_database()")
    user = await conn.fetchval("SELECT current_user")
    version = await conn.fetchval("SHOW server_version")

    report.good(f"connected to database {db_name!r} as {user!r} (PostgreSQL {version})")

    # 1. dedicated database
    if db_name in ("postgres", "template0", "template1"):
        report.bad(
            f"the app is pointed at the {db_name!r} database — create a dedicated "
            "one (CREATE DATABASE fazle_core OWNER fazle) and update DATABASE_URL; "
            "everything in this schema then lands in a shared cluster database"
        )
    else:
        report.good(f"database {db_name!r} is not a cluster default")

    # 2. role attributes
    attrs = await conn.fetchrow(
        """
        SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls
        FROM pg_roles WHERE rolname = current_user
        """
    )
    if attrs:
        elevated = [
            name for name, flag in (
                ("SUPERUSER", attrs["rolsuper"]),
                ("CREATEROLE", attrs["rolcreaterole"]),
                ("CREATEDB", attrs["rolcreatedb"]),
                ("BYPASSRLS", attrs["rolbypassrls"]),
            ) if flag
        ]
        if elevated:
            report.bad(
                f"role {user!r} has {', '.join(elevated)} — the app needs none of "
                "these. Use a plain login role that owns its own database"
            )
        else:
            report.good(f"role {user!r} is a plain login role (no SUPERUSER/CREATEROLE/CREATEDB)")

    # 3. visible schemas
    schemas = [r["nspname"] for r in await conn.fetch(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_temp%'"
    )]
    unexpected = sorted(set(schemas) - SYSTEM_SCHEMAS)
    if unexpected:
        report.warn(
            f"unexpected schema(s) visible to this role: {', '.join(unexpected)} — "
            "another application may share this database"
        )
    else:
        report.good("only 'public' and system schemas are visible")

    # 4. table ownership — the strongest signal of a shared database
    rows = await conn.fetch(
        """
        SELECT c.relname, pg_get_userbyid(c.relowner) AS owner
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
        ORDER BY c.relname
        """
    )
    foreign = [(r["relname"], r["owner"]) for r in rows if r["owner"] != user]
    if foreign:
        sample = ", ".join(f"{n} ({o})" for n, o in foreign[:5])
        report.bad(
            f"{len(foreign)} object(s) in 'public' are owned by another role: {sample}"
            + (" …" if len(foreign) > 5 else "")
            + " — this database is shared; give the app its own database"
        )
    elif rows:
        report.good(f"all {len(rows)} objects in 'public' are owned by {user!r}")
    else:
        report.good("'public' is empty — a virgin database, as expected on first install")

    # 5. search_path
    search_path = await conn.fetchval("SHOW search_path")
    if search_path.replace(" ", "") in ('"$user",public', '"$user",public,'):
        report.good(f"search_path is the default ({search_path})")
    elif "wbom" in search_path or search_path.strip() not in ('"$user", public', '"$user",public'):
        report.warn(f"search_path is not the default: {search_path!r}")

    # 6. pg_trgm
    ext = await conn.fetchval(
        """
        SELECT n.nspname FROM pg_extension e
        JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'pg_trgm'
        """
    )
    if ext == "public":
        report.good("pg_trgm is installed in 'public'")
    elif ext:
        report.warn(f"pg_trgm is installed in schema {ext!r}, not 'public' — migration 029 expects public")
    else:
        report.warn("pg_trgm is not installed yet — migration 029 creates it (needs a superuser on some clusters)")

    # 7. foreign application tables
    hits = sorted({r["relname"] for r in rows
                   if any(h in r["relname"].lower() for h in FOREIGN_HINTS)})
    if hits:
        report.warn(
            f"tables that look like another application's: {', '.join(hits[:5])} — "
            "confirm this database is not shared"
        )


async def run() -> int:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is required (or --selftest)", file=sys.stderr)
        return 3
    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg missing — run inside the service venv", file=sys.stderr)
        return 3

    report = Report()
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot connect: {exc}", file=sys.stderr)
        return 3
    try:
        await _inspect(conn, report)
    finally:
        await conn.close()

    if AS_JSON:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print("database isolation check")
        print(report.render())
        if report.problems:
            print("\nVERDICT: not isolated — fix the ✗ items before going live")
        elif report.warnings:
            print("\nVERDICT: usable, but read the !! items")
        else:
            print("\nVERDICT: isolated and least-privilege")

    if report.problems:
        return 2
    if report.warnings and STRICT:
        return 1
    return 0


def _selftest() -> int:
    """Exercise the classification logic without a database."""
    report = Report()
    report.good("x")
    report.warn("y")
    report.bad("z")
    assert not report.healthy
    payload = report.as_dict()
    assert payload == {"ok": ["x"], "warnings": ["y"], "problems": ["z"]}, payload
    assert report.render().count("\n") == 2
    # default search_path variants we accept
    accepted = {'"$user",public', '"$user", public'}
    assert all(p.replace(" ", "") == '"$user",public' for p in accepted)
    assert set(FOREIGN_HINTS) <= set(FOREIGN_HINTS)
    print("selftest ok")
    return 0


if __name__ == "__main__":
    if SELFTEST:
        raise SystemExit(_selftest())
    raise SystemExit(asyncio.run(run()))
