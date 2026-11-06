"""
Escort Roster Module — __init__.py

Public API:
  sync_program_to_roster(program_id)   — upsert roster entry from wbom_escort_programs
  sync_all_programs()                  — bulk sync all escort programs
  recalculate_entry(program_id)        — recompute shifts/pay for a roster entry
  get_roster_summary()                 — aggregate stats dict
  router                               — FastAPI APIRouter (registered in app/main.py)

DO NOT import from this module in existing modules (no circular deps).
"""
from .db import sync_program_to_roster, sync_all_programs, recalculate_entry, get_roster_summary
from .routes import router

__all__ = [
    "sync_program_to_roster",
    "sync_all_programs",
    "recalculate_entry",
    "get_roster_summary",
    "router",
]
