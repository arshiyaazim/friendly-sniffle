#!/usr/bin/env python3
"""Build the whole schema on a virgin database — `db/migrations` alone cannot.

`db/migrate.py` applies `db/migrations/` and nothing else, and that is correct for
a database that already runs the application: the payroll-engine schema and the
two standalone escort schemas were created years ago and live outside the
numbered sequence. But `db/migrations` *depends* on them —

  * `030_fpe_indexes.sql` indexes `fpe_employees`
  * `046_*` references `fpe_employees`
  * `024_*` references `escort_order_groups`

— so a fresh install that runs only `db/migrations` fails at 030. This script
applies the three sets in dependency order and records every file in the same
`schema_migrations` table `db/migrate.py` uses, so the two runners never apply a
file twice:

  1. ``payroll``  modules/fazle_payroll_engine/migrations/*.sql   (14 files)
  2. ``escort``   migrations/escort_{history,roster}_schema.sql   (2 files)
  3. ``core``     db/migrations/*.sql                             (92 files)

Keys are recorded exactly as `db/migrate.py` records them — bare basenames for
``core`` (it only ever globs that directory), path-scoped for the prerequisites.

Files are applied one per transaction, in dependency rounds: a file that fails
with a "not yet created" class error (undefined table/column/type/function) is
retried in the next round, which is how `db/migrations` is ordered — it is not
topologically sorted, and several later files create objects earlier ones use.

Seven objects exist only in the original production database and are created by
no file in this repository (verified against PostgreSQL 16). When those are the
only reason files cannot apply, the script exits **3** — "the schema is
incomplete, and no amount of re-running will fix it" — rather than pretending the
build succeeded or dying on the first error.

Usage:
    DATABASE_URL=… python scripts/apply_schema.py               # build/upgrade
    DATABASE_URL=… python scripts/apply_schema.py --dry-run     # show the plan
    DATABASE_URL=… python scripts/apply_schema.py --only db     # core set only
    python scripts/apply_schema.py --list                       # plan, no DB

Exit codes:
    0  everything applied (or nothing was pending)
    1  a real error — bad SQL, permissions, unreachable database
    3  incomplete: only the known production-only objects are missing
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PAYROLL_DIR = ROOT / "modules" / "fazle_payroll_engine" / "migrations"
ESCORT_FILES = (
    ROOT / "migrations" / "escort_history_schema.sql",
    ROOT / "migrations" / "escort_roster_schema.sql",
)
CORE_DIR = ROOT / "db" / "migrations"

# Objects that no file in this repository creates. Files depending on them can
# never apply to a virgin database; `exit 3` reports that honestly.
KNOWN_MISSING_OBJECTS = (
    "wbom_candidates",
    "wbom_candidate_conversations",
    "fazle_knowledge_base.value",
    "fazle_knowledge_base.subcategory",
    "fazle_knowledge_base.status",
    "fazle_payment_drafts.employee_phone",
    "role fazle_ai_reader",
    # Added 2026-11-06 by an actual virgin build (PostgreSQL 16.2): payroll
    # migration 008 fails on wbom_employees, 009 on wbom_escort_programs, and
    # identity_brain 004 on fazle_contact_roles. No file in this repository
    # creates any of the three — they are production-only objects like the rest.
    "wbom_employees",
    "wbom_escort_programs",
    "fazle_contact_roles",
)

# Files that are a deliberate no-op on a virgin database.
VIRGIN_NOOP_FILES = frozenset({"086_k32_recruitment_reconciliation.sql"})

# Files written against the *original* production database: their objects were
# added by hand long before the numbered sequence existed. Recorded, not applied.
HISTORICAL_ASSUMPTIONS = frozenset({
    "003_kb_schema_fix_and_recruitment.sql",
    "003c_schema_align.sql",
})

# SQLSTATEs that mean "a dependency does not exist yet" — safe to retry later.
DEFERRABLE_SQLSTATES = frozenset({
    "42P01",  # undefined_table
    "42703",  # undefined_column
    "42704",  # undefined_object (type, role, collation)
    "42883",  # undefined_function
    "42P07",  # duplicate_table (created by an earlier file in a later round)
    "42710",  # duplicate_object
    "42723",  # duplicate_function
    "0A000",  # feature_not_supported (extension variant)
})

MAX_ROUNDS = 12

# ── flags ───────────────────────────────────────────────────────────────────
ARGS = sys.argv[1:]
DRY_RUN = "--dry-run" in ARGS
LIST_ONLY = "--list" in ARGS
ONLY = None
if "--only" in ARGS:
    idx = ARGS.index("--only")
    if idx + 1 >= len(ARGS):
        print("ERROR: --only needs a value (payroll, escort, db, all)", file=sys.stderr)
        raise SystemExit(2)
    ONLY = ARGS[idx + 1]
    if ONLY not in ("payroll", "escort", "db", "all"):
        print(f"ERROR: unknown --only value {ONLY!r}", file=sys.stderr)
        raise SystemExit(2)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── plan ────────────────────────────────────────────────────────────────────
def _plan() -> list[tuple[str, str, str]]:
    """The ordered apply plan: (recorded key, absolute path, group).

    Deterministic, no database access — tests import this directly.
    """
    plan: list[tuple[str, str, str]] = []
    if ONLY in (None, "payroll", "all"):
        for path in sorted(PAYROLL_DIR.glob("*.sql")):
            key = f"modules/fazle_payroll_engine/migrations/{path.name}"
            plan.append((key, str(path), "payroll"))
    if ONLY in (None, "escort", "all"):
        for path in ESCORT_FILES:
            plan.append((f"migrations/{path.name}", str(path), "escort"))
    if ONLY in (None, "db", "all"):
        for path in sorted(CORE_DIR.glob("*.sql")):
            # db/migrate.py records bare basenames — stay compatible.
            plan.append((path.name, str(path), "core"))
    return plan


def _gap_for(message: str) -> str | None:
    """Return the production-only object an error message implicates, if any."""
    lowered = message.lower()
    for gap in KNOWN_MISSING_OBJECTS:
        if gap.lower() in lowered:
            return gap
    return None


def _print_plan(plan) -> None:
    print(f"schema plan: {len(plan)} file(s)")
    for group in ("payroll", "escort", "core"):
        files = [(k, p) for k, p, g in plan if g == group]
        if not files:
            continue
        print(f"  [{group}] {len(files)} file(s)")
        for key, path in files:
            size = Path(path).stat().st_size if Path(path).is_file() else -1
            flag = "" if size > 0 else "   !! MISSING/EMPTY"
            print(f"     {key}  ({size} bytes){flag}")


# ── apply ───────────────────────────────────────────────────────────────────
async def _ensure_bookkeeping(conn) -> set[str]:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT        PRIMARY KEY,
            applied_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    rows = await conn.fetch("SELECT name FROM schema_migrations")
    return {r["name"] for r in rows}


async def _record_only(conn, key: str) -> None:
    """Record a file as applied without executing it (documented no-ops)."""
    await conn.execute(
        "INSERT INTO schema_migrations(name) VALUES($1) ON CONFLICT (name) DO NOTHING",
        key,
    )


async def _apply_file(conn, key: str, path: str) -> None:
    sql = Path(path).read_text(encoding="utf-8")
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES($1)", key)


async def run() -> int:
    plan = _plan()
    if not plan:
        print("nothing to apply — the plan is empty")
        return 0

    if LIST_ONLY or DRY_RUN:
        _print_plan(plan)
        if DRY_RUN and DATABASE_URL:
            print("\n(dry-run: no SQL was executed)")
        return 0

    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is required (--list prints the plan without a DB).",
              file=sys.stderr)
        return 1

    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg is required — run this inside the service venv.",
              file=sys.stderr)
        return 1

    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=15)
    except Exception as exc:  # noqa: BLE001 - surface the real reason
        print(f"ERROR: cannot connect to the database: {exc}", file=sys.stderr)
        return 1

    try:
        applied = await _ensure_bookkeeping(conn)
        pending = [(k, p, g) for k, p, g in plan if k not in applied]
        skipped = len(plan) - len(pending)
        print(f"schema plan: {len(plan)} file(s), {skipped} already recorded, "
              f"{len(pending)} pending")
        if skipped:
            print(f"  skip    {skipped} file(s) recorded in schema_migrations")

        failures: dict[str, tuple[str, str]] = {}   # key -> (group, error)
        gaps_hit: set[str] = set()

        for round_no in range(1, MAX_ROUNDS + 1):
            if not pending:
                break
            progressed = False
            deferred: list[tuple[str, str, str]] = []
            for key, path, group in pending:
                try:
                    await _apply_file(conn, key, path)
                except Exception as exc:  # noqa: BLE001
                    sqlstate = getattr(exc, "sqlstate", None) or ""
                    message = str(exc)
                    if Path(path).name in VIRGIN_NOOP_FILES:
                        # Deliberate guard for a data-repair migration: on a
                        # database that never held production data its
                        # precondition cannot hold. Record it and move on.
                        await _record_only(conn, key)
                        progressed = True
                        failures.pop(key, None)
                        print(f"  noop    {key}  ({message.splitlines()[0][:80]})")
                        continue
                    gap = _gap_for(message)
                    if gap:
                        gaps_hit.add(gap)
                    if sqlstate in DEFERRABLE_SQLSTATES:
                        deferred.append((key, path, group))
                        failures[key] = (group, f"{sqlstate}: {message}")
                        continue
                    print(f"  FAILED  {key}\n          {message}", file=sys.stderr)
                    return 1
                else:
                    progressed = True
                    failures.pop(key, None)
                    print(f"  applied {key}")
            pending = deferred
            if not progressed:
                break
            if pending:
                print(f"  round {round_no}: {len(pending)} file(s) deferred "
                      f"(dependencies not created yet)")

        if not pending:
            print("schema build complete — every file is recorded")
            return 0

        remaining = list(pending)
        blocking = sorted({*gaps_hit,
                           *filter(None, (_gap_for(err) for _g, err in failures.values()))})
        print("")
        print(f"{len(remaining)} file(s) could not be applied:", file=sys.stderr)
        for key, _path, _group in remaining:
            group, err = failures.get(key, ("?", "unknown"))
            print(f"  blocked {key}\n          {err}", file=sys.stderr)

        if blocking:
            print("", file=sys.stderr)
            print("These objects are created by no file in this repository "
                  "(they exist only in the original production database):",
                  file=sys.stderr)
            for gap in blocking:
                print(f"  missing {gap}", file=sys.stderr)
            print("", file=sys.stderr)
            print("Nothing left can fix this. Restore a dump of the production "
                  "database, or create the objects above and re-run.", file=sys.stderr)
            return 3

        print("", file=sys.stderr)
        print("No known-missing object explains this — inspect the errors above.",
              file=sys.stderr)
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
