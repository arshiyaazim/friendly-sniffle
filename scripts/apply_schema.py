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
applies four sets in dependency order and records every file in the same
`schema_migrations` table `db/migrate.py` uses, so the two runners never apply a
file twice:

  0. ``baseline`` db/migrations/000_virgin_baseline.sql            (1 file)
  1. ``payroll``  modules/fazle_payroll_engine/migrations/*.sql   (14 files)
  2. ``escort``   migrations/escort_{history,roster}_schema.sql    (2 files)
  3. ``core``     db/migrations/*.sql                             (92 files)

The baseline is the part that makes a *virgin* database possible: the numbered
history starts mid-story (it ALTERs tables it never creates), so
`000_virgin_baseline.sql` creates the 29 pre-history tables — members, contacts,
drafts, knowledge base, admins/roles — plus `wbom_relation_types`,
`fazle_outbound_queue` and the `fazle_ai_reader` role. It is idempotent and
harmless on an existing database. With it, a fresh PostgreSQL (with `pg_trgm`
available) reaches 109/109 recorded files and exit code 0.

Keys are recorded exactly as `db/migrate.py` records them — bare basenames for
``core`` (it only ever globs that directory), path-scoped for the prerequisites.

Files are applied one per transaction, in dependency rounds: a file that fails
with a "not yet created" class error (undefined table/column/type/function) is
retried in the next round, which is how `db/migrations` is ordered — it is not
topologically sorted, and several later files create objects earlier ones use.

When a file still cannot apply because an object is genuinely absent (an
extension is not installed, or a hand-made object exists only in production), the
script exits **3** — "the schema is incomplete, and no amount of re-running will
fix it" — rather than pretending the build succeeded or dying on the first error.
A *capability* failure (`0A000` / `42704` / `42883`, e.g. `CREATE EXTENSION
pg_trgm` without contrib) is salvaged statement by statement and reported as
`partial`, so one missing extension does not cost the rest of the file.

Usage:
    DATABASE_URL=… python scripts/apply_schema.py               # build/upgrade
    DATABASE_URL=… python scripts/apply_schema.py --dry-run     # show the plan
    DATABASE_URL=… python scripts/apply_schema.py --only db          # core set only
    DATABASE_URL=… python scripts/apply_schema.py --only baseline    # pre-history only
    DATABASE_URL=… python scripts/apply_schema.py --only foundation  # baseline+payroll+escort
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
# Run as `python scripts/apply_schema.py`, sys.path[0] is scripts/ — the shared
# SQL splitter lives in the repo root package, so put the root back on the path.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAYROLL_DIR = ROOT / "modules" / "fazle_payroll_engine" / "migrations"
ESCORT_FILES = (
    ROOT / "migrations" / "escort_history_schema.sql",
    ROOT / "migrations" / "escort_roster_schema.sql",
)
CORE_DIR = ROOT / "db" / "migrations"

# The pre-migration baseline: 29 tables that predate the numbered sequence plus
# the fazle_ai_reader role. Applied before the payroll set, because payroll
# migration 008 references wbom_employees. db/migrate.py also picks it up (it
# sorts first and is idempotent), which keeps a single definition.
BASELINE_FILE = CORE_DIR / "000_virgin_baseline.sql"

# Objects that a virgin database would be missing if the baseline did not provide
# them. This list is kept as a *checklist* rather than a permanent gap report:
# 000_virgin_baseline.sql now creates every one of them, and a virgin build
# finishes with nothing missing. It stays because the list is what the baseline
# was written from — `tests/unit/test_apply_schema_plan.py` asserts each name is
# present in the baseline file, so deleting a table from the baseline breaks a
# test instead of a fresh install.
BASELINE_PROVIDED_OBJECTS = (
    "wbom_candidates",
    "wbom_candidate_conversations",
    "wbom_employees",            # payroll migration 008
    "wbom_escort_programs",      # payroll migration 009
    "fazle_contact_roles",       # identity_brain migration 004
    "fazle_knowledge_base.value",
    "fazle_knowledge_base.subcategory",
    "fazle_knowledge_base.status",
    "fazle_payment_drafts.employee_phone",
    "role fazle_ai_reader",
)

# Backwards-compatible alias: older code and tests referenced this name.
KNOWN_MISSING_OBJECTS = BASELINE_PROVIDED_OBJECTS

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
    "42701",  # duplicate_column (a historical file re-adding what 002 already has)
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
        print("ERROR: --only needs a value (baseline, payroll, escort, db, foundation, all)",
              file=sys.stderr)
        raise SystemExit(2)
    ONLY = ARGS[idx + 1]
    if ONLY not in ("baseline", "payroll", "escort", "db", "foundation", "all"):
        print(f"ERROR: unknown --only value {ONLY!r}", file=sys.stderr)
        raise SystemExit(2)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── plan ────────────────────────────────────────────────────────────────────
def _plan() -> list[tuple[str, str, str]]:
    """The ordered apply plan: (recorded key, absolute path, group).

    Deterministic, no database access — tests import this directly.
    """
    plan: list[tuple[str, str, str]] = []
    if ONLY in (None, "baseline", "foundation", "all") and BASELINE_FILE.is_file():
        plan.append((BASELINE_FILE.name, str(BASELINE_FILE), "baseline"))
    # `--only baseline` means the baseline file alone. It used to drag the payroll
    # set in with it, so the flag actually meant "everything before db/migrations";
    # `--only foundation` now says that explicitly.
    if ONLY in (None, "payroll", "foundation", "all"):
        for path in sorted(PAYROLL_DIR.glob("*.sql")):
            key = f"modules/fazle_payroll_engine/migrations/{path.name}"
            plan.append((key, str(path), "payroll"))
    if ONLY in (None, "escort", "foundation", "all"):
        for path in ESCORT_FILES:
            plan.append((f"migrations/{path.name}", str(path), "escort"))
    if ONLY in (None, "db", "all"):
        for path in sorted(CORE_DIR.glob("*.sql")):
            if path == BASELINE_FILE:
                continue          # already planned above, must run first
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
    for group in ("baseline", "payroll", "escort", "core"):
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


# Errors that mean "this optional capability is not present on this server",
# not "the migration is wrong". A file that hits one of these is retried
# statement by statement so the other 200 statements still land.
_CAPABILITY_STATES = frozenset({
    "0A000",  # feature_not_supported — extension (pg_trgm) not installed
    "42704",  # undefined_object — gin_trgm_ops operator class
    "42883",  # undefined_function — similarity() etc.
})
_SOFT_STATEMENT_STATES = _CAPABILITY_STATES | frozenset({
    "42P01",  # undefined_table — depends on a skipped statement
    "42703",  # undefined_column
    "42P07",  # duplicate_table
    "42701",  # duplicate_column
    "42710",  # duplicate_object
})


async def _apply_statements(conn, key: str, path: str) -> list[tuple[str, str, str]]:
    """Apply a file statement by statement; return the skipped ones.

    Used only when the whole-file transaction failed on a *soft* error (a
    missing optional extension, typically pg_trgm): one unsupported statement
    must not throw away the 240 correct ones — 029 alone creates ~100 tables.
    """
    from shared.sql_split import split_sql

    skipped: list[tuple[str, str, str]] = []
    for statement in split_sql(Path(path).read_text(encoding="utf-8")):
        body = statement.strip()
        if not body or all(line.strip().startswith("--") or not line.strip()
                           for line in body.splitlines()):
            continue
        try:
            async with conn.transaction():
                await conn.execute(statement)
        except Exception as exc:  # noqa: BLE001
            state = getattr(exc, "sqlstate", None) or ""
            if state not in _SOFT_STATEMENT_STATES:
                raise
            skipped.append((body.splitlines()[0][:70], state, str(exc).splitlines()[0]))
    await _record_only(conn, key)
    return skipped


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
        capability_gaps: list[tuple[str, str, str]] = []

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
                    basename = Path(path).name
                    if basename in HISTORICAL_ASSUMPTIONS and sqlstate in DEFERRABLE_SQLSTATES:
                        # These files describe a production database that had
                        # drifted from the migrations (a column under a different
                        # name, a table created by hand). On a database built
                        # from this repository the assumption is already
                        # satisfied or irrelevant; record it and move on.
                        await _record_only(conn, key)
                        progressed = True
                        failures.pop(key, None)
                        print(f"  assumed {key}  ({sqlstate}: {message.splitlines()[0][:70]})")
                        continue
                    if basename in VIRGIN_NOOP_FILES:
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
                    if sqlstate in _CAPABILITY_STATES:
                        # Whole-file transaction cannot run (a missing optional
                        # extension); salvage the statements that do not need it
                        # instead of losing the other 240.
                        try:
                            skipped = await _apply_statements(conn, key, path)
                        except Exception as inner:  # noqa: BLE001
                            print(f"  FAILED  {key}\n          {inner}", file=sys.stderr)
                            return 1
                        progressed = True
                        failures.pop(key, None)
                        capability_gaps.append((key, sqlstate, message.splitlines()[0]))
                        print(f"  partial {key}  ({len(skipped)} statement(s) skipped)")
                        for head, state, msg in skipped:
                            print(f"          skip [{state}] {head}")
                        continue
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
            if capability_gaps:
                print("")
                print("schema build complete — every file is recorded, "
                      f"but {len(capability_gaps)} file(s) ran partially:")
                for key, state, msg in capability_gaps:
                    print(f"  partial {key}: [{state}] {msg}")
                print("  Install the missing extension (on Debian/Ubuntu: "
                      "postgresql-contrib) and re-run this script on a fresh "
                      "database for full parity.")
            else:
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
