"""
Fazle Payroll Engine (FPE) — Module entry point.

Start/stop lifecycle:
  start_fpe()  — called from app/main.py lifespan on startup
  stop_fpe()   — called from app/main.py lifespan on shutdown

Registers all background workers. The FastAPI router is imported and
registered separately in app/main.py as:

    from modules.fazle_payroll_engine.routes import router as fpe_router
    app.include_router(fpe_router)
"""
from __future__ import annotations

import logging

log = logging.getLogger("fazle.fpe")


async def start_fpe(chat_jids: list[str] | None = None) -> None:
    """Initialize FPE tables and start all background workers."""
    from app.database import execute

    # Run migrations inline (idempotent IF NOT EXISTS — safe every startup).
    # Order matters: 001 creates base tables, 002 adds normalization layer.
    from pathlib import Path
    migrations_dir = Path(__file__).parent / "migrations"
    for migration_path in sorted(migrations_dir.glob("*.sql")):
        migration_sql = migration_path.read_text()
        for stmt in _split_sql(migration_sql):
            try:
                await execute(stmt)
            except Exception as exc:
                log.warning(
                    "[fpe] migration %s stmt skipped: %s — %s",
                    migration_path.name, stmt[:60], exc,
                )

    log.info("[fpe] schema ready")

    # Start workers
    from .workers import start_workers
    await start_workers(chat_jids)
    log.info("[fpe] startup complete")


async def stop_fpe() -> None:
    """Cancel all FPE background workers."""
    from .workers import stop_workers
    await stop_workers()
    log.info("[fpe] shutdown complete")


def _split_sql(sql: str) -> list[str]:
    """Split a SQL file into individual statements, skipping blanks and comments."""
    stmts = []
    current: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        current.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt:
                stmts.append(stmt)
            current = []
    if current:
        leftover = "\n".join(current).strip()
        if leftover:
            stmts.append(leftover)
    return stmts
