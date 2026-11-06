"""
shared.draft — Payment draft lifecycle helpers.

Centralises:
  • Stale-draft expiry (24-hour TTL by default)
  • Guard against duplicate escort payment drafts
  • Historical-message guard (never generate drafts for imported history)
  • "Already replied" guard (no draft when admin already confirmed)
  • Draft active/inactive status check
  • Force-expire all active drafts for a program (admin confirmation flow)

All functions are thin DB wrappers — no business logic beyond what is
described in their docstring.  They NEVER raise; errors are logged and a
safe default is returned.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.database import fetch_one, fetch_val, execute

log = logging.getLogger("fazle.shared.draft")

# ── Status constants ──────────────────────────────────────────────────────────

PENDING   = "pending"
APPROVED  = "approved"
CANCELLED = "cancelled"
EXPIRED   = "expired"
REVERSED  = "reversed"
REJECTED  = "rejected"

TERMINAL_STATUSES = {APPROVED, CANCELLED, EXPIRED, REVERSED, REJECTED}
ACTIVE_STATUSES   = {PENDING}


# ── Expiry ────────────────────────────────────────────────────────────────────

async def expire_stale_drafts(ttl_hours: int = 24) -> int:
    """
    Mark pending drafts as 'expired' when:
      - expires_at IS NOT NULL and expires_at < NOW()          OR
      - expires_at IS NULL and created_at < NOW() - ttl_hours  (legacy rows)

    Returns the number of rows updated.
    Never raises.
    """
    try:
        count = await fetch_val(
            """
            WITH expired AS (
                UPDATE fazle_payment_drafts
                SET    status     = 'expired',
                       updated_at = NOW()
                WHERE  status = 'pending'
                  AND  (
                           (expires_at IS NOT NULL AND expires_at < NOW())
                        OR (expires_at IS NULL    AND created_at < NOW() - ($1 || ' hours')::INTERVAL)
                       )
                RETURNING id
            )
            SELECT COUNT(*) FROM expired
            """,
            str(ttl_hours),
        )
        n = int(count or 0)
        if n:
            log.info(f"[draft] expired {n} stale draft(s) (ttl={ttl_hours}h)")
        return n
    except Exception as e:
        log.error(f"[draft] expire_stale_drafts error: {e}")
        return 0


# ── Active check ──────────────────────────────────────────────────────────────

async def is_draft_active(draft_id: int) -> bool:
    """
    Return True if the draft exists and its status is 'pending'.
    Returns False on any error or if draft does not exist.
    """
    try:
        row = await fetch_one(
            "SELECT status FROM fazle_payment_drafts WHERE id = $1",
            draft_id,
        )
        if not row:
            return False
        return row["status"] in ACTIVE_STATUSES
    except Exception as e:
        log.error(f"[draft] is_draft_active({draft_id}) error: {e}")
        return False


# ── Escort payment draft dedup guard ─────────────────────────────────────────

async def find_existing_escort_draft(employee_id: int, escort_program_id: int) -> Optional[int]:
    """
    Check whether a non-terminal payment draft already exists for the given
    employee_id + escort_program_id pair.

    Returns the existing draft id, or None if no duplicate exists.
    Never raises.
    """
    try:
        row = await fetch_one(
            """
            SELECT id FROM fazle_payment_drafts
            WHERE  employee_id        = $1
              AND  escort_program_id  = $2
              AND  status NOT IN ('cancelled', 'expired', 'reversed', 'rejected')
            ORDER BY id DESC
            LIMIT 1
            """,
            employee_id, escort_program_id,
        )
        if row:
            log.info(f"[draft] duplicate guard hit: emp={employee_id} prog={escort_program_id} draft={row['id']}")
            return row["id"]
        return None
    except Exception as e:
        log.error(f"[draft] find_existing_escort_draft error: {e}")
        return None


async def mark_draft_expired(draft_id: int) -> bool:
    """
    Manually force-expire a single draft.
    Returns True on success, False on error.
    """
    try:
        await execute(
            """UPDATE fazle_payment_drafts
               SET    status     = 'expired',
                      updated_at = NOW()
               WHERE  id = $1 AND status = 'pending'""",
            draft_id,
        )
        return True
    except Exception as e:
        log.error(f"[draft] mark_draft_expired({draft_id}) error: {e}")
        return False


# ── Historical-message and "already replied" guards ──────────────────────────

async def should_generate_draft(
    chat_jid: str,
    msg_timestamp: Optional[datetime],
    *,
    historical_cutoff_hours: int = 2,
) -> bool:
    """
    Return True only when it is safe to generate a new draft for this chat.

    Rules applied in order:
    1. Historical guard — message older than `historical_cutoff_hours` at time
       of processing → False.  This prevents imported WA history from
       triggering drafts hours/days after the fact.
    2. Already-replied guard — if any outbound (is_from_me) message was sent
       to this chat AFTER the incoming message, an admin has already handled
       it → False.

    Returns True (allow draft) on any DB error to fail open.
    """
    # 1. Historical guard
    if msg_timestamp is not None:
        now = datetime.now(timezone.utc)
        ts = (
            msg_timestamp.replace(tzinfo=timezone.utc)
            if msg_timestamp.tzinfo is None
            else msg_timestamp
        )
        age_hours = (now - ts).total_seconds() / 3600.0
        if age_hours > historical_cutoff_hours:
            log.info(
                "[draft] skip — historical message age=%.1fh (cutoff=%dh) chat=%s",
                age_hours, historical_cutoff_hours, chat_jid,
            )
            return False

    # 2. Already-replied guard
    try:
        replied = await fetch_val(
            """
            SELECT 1 FROM fpe_wa_messages
            WHERE  chat_jid    = $1
              AND  is_from_me  = TRUE
              AND  timestamp_wa > $2
            LIMIT 1
            """,
            chat_jid,
            msg_timestamp or datetime.min.replace(tzinfo=timezone.utc),
        )
        if replied:
            log.info("[draft] skip — admin already replied to chat=%s", chat_jid)
            return False
    except Exception as exc:
        log.warning("[draft] should_generate_draft DB check failed: %s", exc)
        # Fail open — allow draft creation when we cannot confirm

    return True


async def expire_program_drafts(employee_id: int, escort_program_id: int) -> int:
    """
    Force-expire ALL pending drafts for a given employee + escort program.

    Called when an admin sends a confirmation or cancellation, so stale drafts
    are cleaned up automatically rather than accumulating.

    Returns the number of rows expired.  Never raises.
    """
    try:
        count = await fetch_val(
            """
            WITH expired AS (
                UPDATE fazle_payment_drafts
                SET    status     = 'expired',
                       updated_at = NOW()
                WHERE  employee_id       = $1
                  AND  escort_program_id = $2
                  AND  status            = 'pending'
                RETURNING id
            )
            SELECT COUNT(*) FROM expired
            """,
            employee_id, escort_program_id,
        )
        n = int(count or 0)
        if n:
            log.info(
                "[draft] expired %d draft(s) for emp=%d prog=%d",
                n, employee_id, escort_program_id,
            )
        return n
    except Exception as exc:
        log.error("[draft] expire_program_drafts error: %s", exc)
        return 0
