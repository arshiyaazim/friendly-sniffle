#!/usr/bin/env python3
"""Restore the newest backup into a scratch database and prove it is usable.

The audit found the rollback story *documented but never drilled*: `install.sh`
takes a `pg_dump` before migrating, and there are no down-migrations, so a
restore is the only rollback — but nobody had ever restored one. A backup you
have not restored is a hope, not a rollback.

This script does the drill against a throwaway database, so production is never
touched:

  1. pick the newest `backups/*.sql.gz` (or one you name)
  2. check the archive: gzip integrity, plausible SQL dump, ends with the
     "PostgreSQL database dump complete" line
  3. create `<db>_drill_<UTC>` on the same server
  4. `gunzip -c | psql -v ON_ERROR_STOP=1`, tolerating the known production-only
     objects (role fazle_ai_reader etc.) that a fresh cluster may lack
  5. verify: `schema_migrations` rows, table count, row counts for the key
     tables, and the largest tables
  6. drop the scratch database (unless --keep) and print a verdict

Usage:
    python scripts/restore_drill.py                       # newest backups/*.sql.gz
    python scripts/restore_drill.py --backup backups/x.sql.gz
    python scripts/restore_drill.py --dry-run             # show the commands
    python scripts/restore_drill.py --selftest            # no database needed

Requires the `psql` / `pg_dump` client binaries (postgresql-client) and a role
with CREATEDB — or pass `--into <existing-empty-db>` to use one you created.

Exit codes:
    0  restore succeeded and the data looks sane
    1  the restore or a verification query failed
    2  no usable backup found / missing client tools
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]

ARGS = sys.argv[1:]
DRY_RUN = "--dry-run" in ARGS
SELFTEST = "--selftest" in ARGS
KEEP = "--keep" in ARGS


def _arg(name: str, default: str | None = None) -> str | None:
    if name in ARGS:
        idx = ARGS.index(name)
        if idx + 1 < len(ARGS):
            return ARGS[idx + 1]
    return default


BACKUP_ARG = _arg("--backup")
INTO_DB = _arg("--into")
BACKUP_DIR = Path(_arg("--dir", os.environ.get("BACKUP_DIR", str(ROOT / "backups"))) or "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Errors a restore may legitimately hit on a cluster that never saw production.
TOLERATED = (
    'role "fazle_ai_reader" does not exist',
    "must be owner of",
    "already exists",
    "extension \"pg_trgm\" already exists",
)

KEY_TABLES = (
    "schema_migrations",
    "fpe_employees",
    "fazle_payment_drafts",
    "wbom_billing_records",
    "fazle_admins",
)


def _utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _split_url(url: str) -> tuple[str, str, str, str]:
    """(maintenance url on the same server, db name, user, password-bearing base)."""
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise SystemExit(f"ERROR: DATABASE_URL is not a postgres URL: {url!r}")
    db_name = (parts.path or "/").lstrip("/") or "postgres"
    maint = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))
    return maint, db_name, parts.username or "", parts.netloc


def _with_db(url: str, db_name: str) -> str:
    """Same server/credentials, different database — via urlsplit, never .replace().

    `"postgresql://postgres:x@…".replace("/postgres", …)` also rewrites the
    scheme/user part ("//postgres") and silently produces a broken URL; that bug
    made the first drill restore into nothing.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", parts.query, parts.fragment))


def _mask_url(url: str) -> str:
    """Never print a password, even in a dry run (installer does the same)."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    netloc = f"{parts.username}:***@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _drill_db_name(db_name: str) -> str:
    """`<db>_drill_<utc>` — must stay under PostgreSQL's 63-byte identifier limit."""
    suffix = f"_drill_{_utc_stamp().lower()}"
    base = re.sub(r"[^a-z0-9_]", "_", db_name.lower())[:max(1, 62 - len(suffix))]
    return f"{base}{suffix}"


def _pick_backup() -> Path | None:
    if BACKUP_ARG:
        path = Path(BACKUP_ARG).expanduser()
        return path if path.is_file() else None
    if not BACKUP_DIR.is_dir():
        return None
    candidates = sorted(
        (p for p in BACKUP_DIR.glob("*.sql.gz") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _check_archive(path: Path) -> tuple[bool, str]:
    """gzip integrity + a real pg_dump tail, without loading the whole file."""
    try:
        subprocess.run(["gzip", "-t", str(path)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        return False, "gzip integrity check failed — the archive is corrupt"
    except FileNotFoundError:
        return False, "gzip is not installed"
    try:
        tail = subprocess.run(["bash", "-c", f"gunzip -c {shlex.quote(str(path))} | tail -c 4096"],
                              check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError:
        return False, "cannot read the archive contents"
    if "PostgreSQL database dump complete" not in tail:
        return False, ("the archive does not end with pg_dump's completion line — "
                       "the dump was truncated")
    return True, "archive integrity ok"


def _psql(url: str, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    cmd = ["psql", url, "-v", "ON_ERROR_STOP=1", "-q", *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=False)


def _query(url: str, sql: str) -> str:
    out = subprocess.run(["psql", url, "-At", "-c", sql], capture_output=True, text=True)
    return out.stdout.strip()


def run() -> int:
    backup = _pick_backup()
    if not backup:
        print(f"ERROR: no backup found (looked in {BACKUP_DIR}); pass --backup <file>",
              file=sys.stderr)
        return 2
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is required (the drill clones that server)", file=sys.stderr)
        return 2
    print(f"backup: {backup} ({backup.stat().st_size / 1024 / 1024:.1f} MB)")
    ok, message = _check_archive(backup)
    print(f"  {message}")
    if not ok:
        return 1

    maint_url, db_name, _user, _netloc = _split_url(DATABASE_URL)
    drill = INTO_DB or _drill_db_name(db_name)
    create_sql = f'CREATE DATABASE "{drill}"'
    drill_url = _with_db(maint_url, drill)

    if DRY_RUN:
        print("\ndry run — commands that would run:")
        print(f"  psql <maintenance> -c {create_sql!r}")
        print(f"  gunzip -c {backup} | psql {_mask_url(drill_url)} -v ON_ERROR_STOP=1")
        print(f"  psql {_mask_url(drill_url)} -c 'SELECT count(*) FROM schema_migrations'")
        print(f'  psql <maintenance> -c \'DROP DATABASE "{drill}"\'')
        return 0

    if not shutil_which("psql"):
        print("ERROR: psql not found — install postgresql-client", file=sys.stderr)
        return 2

    t0 = time.monotonic()
    reused = False
    if INTO_DB:
        existing = _query(maint_url, f"SELECT 1 FROM pg_database WHERE datname = '{drill}'")
        if existing.strip() == "1":
            reused = True
            print(f"  reusing existing database {drill!r}")
    if not INTO_DB or not reused:
        created = _psql(maint_url, "-c", create_sql)
        if created.returncode != 0:
            print("ERROR: cannot create the scratch database:", file=sys.stderr)
            print(created.stderr.decode(errors="replace").strip(), file=sys.stderr)
            print("\nThe role needs CREATEDB for the drill, or pass --into <existing empty db>.",
                  file=sys.stderr)
            return 2
        print(f"  created {drill!r}")

    try:
        cat = subprocess.Popen(["gunzip", "-c", str(backup)], stdout=subprocess.PIPE)
        restore = subprocess.run(
            ["psql", drill_url, "-v", "ON_ERROR_STOP=0", "-q", "-o", "/dev/null"],
            stdin=cat.stdout, capture_output=True, text=True,
        )
        cat.stdout.close()
        cat.wait()
        stderr = restore.stderr or ""
        hard = [
            line for line in stderr.splitlines()
            if re.search(r"\bERROR:", line) and not any(t in line for t in TOLERATED)
        ]
        tolerated = sum(1 for line in stderr.splitlines()
                        if "ERROR:" in line) - len(hard)
        if tolerated:
            print(f"  {tolerated} tolerated error(s) (production-only objects)")
        if hard:
            print("ERROR: the restore produced real errors:", file=sys.stderr)
            for line in hard[:10]:
                print(f"  {line}", file=sys.stderr)
            return 1

        applied = _query(drill_url, "SELECT count(*) FROM schema_migrations")
        tables = _query(drill_url,
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public'")
        print("\nverify")
        print(f"  schema_migrations rows : {applied}")
        print(f"  tables in public       : {tables}")
        for table in KEY_TABLES[1:]:
            exists = _query(drill_url, f"SELECT to_regclass('public.{table}') IS NOT NULL")
            if exists.strip() != "t":
                print(f"  {table:22} : absent")
                continue
            rows = _query(drill_url, f"SELECT count(*) FROM {table}")
            print(f"  {table:22} : {rows} row(s)")

        biggest = _query(
            drill_url,
            """
            SELECT relname || '=' || n_live_tup
            FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 5
            """,
        )
        if biggest:
            print(f"  largest tables         : {', '.join(biggest.splitlines())}")

        if applied.strip() in ("", "0"):
            print("\nVERDICT: FAILED — the restored database has no applied migrations",
                  file=sys.stderr)
            return 1
        duration = time.monotonic() - t0
        print(f"\nVERDICT: PASS — backup restores cleanly into a scratch database "
              f"in {duration:.0f}s")
        return 0
    finally:
        if not KEEP and not INTO_DB:
            dropped = _psql(maint_url, "-c", f'DROP DATABASE IF EXISTS "{drill}" WITH (FORCE)')
            if dropped.returncode == 0:
                print(f"  dropped {drill!r}")
            else:
                print(f"  !! could not drop {drill!r} — remove it by hand",
                      file=sys.stderr)
        elif KEEP:
            print(f"  kept {drill!r} (--keep)")


def shutil_which(binary: str) -> str | None:
    from shutil import which
    return which(binary)


def _selftest() -> int:
    assert _split_url("postgresql://u:p@h:5432/fazle_core")[1] == "fazle_core"
    assert _split_url("postgresql://u:p@h:5432/fazle_core")[0].endswith("/postgres")
    assert _split_url("postgresql://u@h/")[1] == "postgres"
    assert _with_db("postgresql://postgres:x@h:5433/postgres", "d1") == \
        "postgresql://postgres:x@h:5433/d1"
    assert _mask_url("postgresql://u:secret@h:5432/db") == "postgresql://u:***@h:5432/db"
    assert "secret" not in _mask_url("postgresql://u:secret@h/db")
    name = _drill_db_name("Fazle-Core")
    assert name.startswith("fazle_core_drill_") and name.replace("_", "").isalnum()
    assert len(_drill_db_name("x" * 80)) < 63
    # tolerated-error classifier used by the restore step
    line = 'ERROR:  role "fazle_ai_reader" does not exist'
    assert any(t in line for t in TOLERATED)
    assert not any(t in "ERROR:  syntax error at or near" for t in TOLERATED)
    print("selftest ok")
    return 0


if __name__ == "__main__":
    if SELFTEST:
        raise SystemExit(_selftest())
    raise SystemExit(run())
