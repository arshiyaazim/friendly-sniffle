from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from app.config import get_settings
from app.database import execute, fetch_all, fetch_one, fetch_val
from modules import observability as obs
from modules.draft_quality import check_draft_quality
from modules.number_identity import phone_last10

log = logging.getLogger("fazle.reviewed_reply_memory")

_DEFAULT_DRAFT_TYPE = "generic"
_DEFAULT_STATUS = "active"
_DISABLED_STATUS = "disabled"
_UNSAFE_DRAFT_TYPES = frozenset({"attendance", "payment", "gap_action"})

# Prefixes that mark admin-command text, not customer-facing replies
_UNSAFE_REPLY_PREFIXES: tuple[str, ...] = (
    "approve ", "reject ", "paid ", "advance ",
    "escortconfirm ", "payroll ", "release ", "backup ",
)
# Substrings that indicate credential/system leakage
_UNSAFE_REPLY_SUBSTRINGS: tuple[str, ...] = (
    "api_key", "password", "token=", "secret", ".env",
    "sudo ", "systemctl", "/home/azim",
)


def _has_unsafe_content(text: str) -> bool:
    """Return True if text contains admin commands or credential patterns."""
    lower = (text or "").strip().lower()
    for prefix in _UNSAFE_REPLY_PREFIXES:
        if lower.startswith(prefix):
            return True
    for substr in _UNSAFE_REPLY_SUBSTRINGS:
        if substr in lower:
            return True
    return False


def _feature_enabled() -> bool:
    return bool(get_settings().reviewed_reply_memory_enabled)


def _normalize_text(text: str) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip().lower())
    return value[:1000]


def normalize_lookup_context(
    *,
    sender_phone: str,
    intent: str,
    role: str = "",
    draft_type: str = _DEFAULT_DRAFT_TYPE,
    language: str = "",
    candidate_text: str = "",
) -> dict[str, str]:
    return {
        "intent": (intent or "").strip().lower(),
        "role": (role or "").strip().lower(),
        "draft_type": (draft_type or _DEFAULT_DRAFT_TYPE).strip().lower() or _DEFAULT_DRAFT_TYPE,
        "language": (language or "").strip().lower(),
        "last10_phone": phone_last10(sender_phone or ""),
        "normalized_trigger_text": _normalize_text(candidate_text),
    }


def _eligible_draft_type(draft_type: str) -> bool:
    normalized = (draft_type or _DEFAULT_DRAFT_TYPE).strip().lower() or _DEFAULT_DRAFT_TYPE
    return normalized not in _UNSAFE_DRAFT_TYPES


def _match_scope(last10: str, role: str, draft_type: str) -> str:
    if last10 and role and draft_type:
        return "intent_role_phone"
    return "intent_role"


async def create_or_update_from_edit(
    *,
    draft_row: dict[str, Any],
    new_text: str,
    admin_phone: str,
    role: str = "",
    language: str = "",
) -> Optional[dict[str, Any]]:
    if not _feature_enabled():
        return None

    ok, reason = check_draft_quality(new_text)
    if not ok:
        obs.inc("reviewed_reply_lookup_total", labels={"result": "blocked"})
        log.info("[reviewed] skip create source_draft=%s reason=%s", draft_row.get("id"), reason)
        return None

    if _has_unsafe_content(new_text):
        obs.inc("reviewed_reply_lookup_total", labels={"result": "blocked"})
        log.info("[reviewed] skip create source_draft=%s reason=unsafe_content", draft_row.get("id"))
        return None

    intent = str(draft_row.get("intent") or "").strip().lower()
    if not intent:
        obs.inc("reviewed_reply_lookup_total", labels={"result": "blocked"})
        return None

    draft_type = str(draft_row.get("draft_type") or _DEFAULT_DRAFT_TYPE).strip().lower() or _DEFAULT_DRAFT_TYPE
    if not _eligible_draft_type(draft_type):
        obs.inc("reviewed_reply_lookup_total", labels={"result": "blocked"})
        return None

    recipient_phone = str(draft_row.get("recipient") or "")
    last10 = phone_last10(recipient_phone)
    role_value = (role or _extract_role(draft_row)).strip().lower()
    meta = {
        "source_reason": "edited_draft",
        "original_reply_text": draft_row.get("reply_text") or "",
        "created_from_status": draft_row.get("status") or "pending",
        "guard_version": "b26",
    }
    match_scope = _match_scope(last10, role_value, draft_type)
    normalized_trigger_text = _normalize_text(str(draft_row.get("trigger_text") or ""))

    existing = await fetch_one(
        """
        SELECT *
        FROM fazle_reviewed_replies
        WHERE intent = $1
          AND COALESCE(role, '') = $2
          AND draft_type = $3
          AND COALESCE(last10_phone, '') = $4
          AND status = 'active'
        ORDER BY id DESC
        LIMIT 1
        """,
        intent,
        role_value,
        draft_type,
        last10,
    )

    if existing:
        await execute(
            """
            UPDATE fazle_reviewed_replies
            SET reply_text = $1,
                recipient_phone = $2,
                language = $3,
                normalized_trigger_text = $4,
                created_by = $5,
                updated_at = NOW(),
                meta = $6::jsonb
            WHERE id = $7
            """,
            new_text,
            recipient_phone,
            language,
            normalized_trigger_text,
            admin_phone,
            json.dumps(meta),
            existing["id"],
        )
        obs.inc("reviewed_reply_updated_total")
        return await fetch_one("SELECT * FROM fazle_reviewed_replies WHERE id = $1", existing["id"])

    reviewed_id = await fetch_val(
        """
        INSERT INTO fazle_reviewed_replies (
            source_draft_id, source, intent, draft_type, role,
            recipient_phone, last10_phone, language, normalized_trigger_text,
            match_scope, reply_text, status, created_by, meta
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9,
            $10, $11, $12, $13, $14::jsonb
        )
        RETURNING id
        """,
        draft_row.get("id"),
        draft_row.get("source") or "",
        intent,
        draft_type,
        role_value,
        recipient_phone,
        last10,
        language,
        normalized_trigger_text,
        match_scope,
        new_text,
        _DEFAULT_STATUS,
        admin_phone,
        json.dumps(meta),
    )
    obs.inc("reviewed_reply_created_total")
    return await fetch_one("SELECT * FROM fazle_reviewed_replies WHERE id = $1", reviewed_id)


async def lookup_reviewed_reply(
    *,
    sender_phone: str,
    intent: str,
    role: str = "",
    draft_type: str = _DEFAULT_DRAFT_TYPE,
    language: str = "",
    candidate_text: str = "",
    touch: bool = True,
) -> Optional[dict[str, Any]]:
    if not _feature_enabled():
        obs.inc("reviewed_reply_lookup_total", labels={"result": "blocked"})
        return None

    context = normalize_lookup_context(
        sender_phone=sender_phone,
        intent=intent,
        role=role,
        draft_type=draft_type,
        language=language,
        candidate_text=candidate_text,
    )
    if not context["intent"] or not context["role"] or not _eligible_draft_type(context["draft_type"]):
        if touch:
            obs.inc("reviewed_reply_lookup_total", labels={"result": "blocked"})
        return None

    attempts = [
        (context["intent"], context["role"], context["draft_type"], context["last10_phone"]),
        (context["intent"], context["role"], context["draft_type"], ""),
        (context["intent"], context["role"], "", ""),
    ]
    seen: set[tuple[str, str, str, str]] = set()
    for current_intent, current_role, current_draft_type, current_last10 in attempts:
        key = (current_intent, current_role, current_draft_type, current_last10)
        if key in seen:
            continue
        seen.add(key)
        row = await _lookup_scope(
            intent=current_intent,
            role=current_role,
            draft_type=current_draft_type,
            last10_phone=current_last10,
        )
        if row:
            if touch:
                await execute(
                    """
                    UPDATE fazle_reviewed_replies
                    SET usage_count = usage_count + 1,
                        last_used_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    row["id"],
                )
            if touch:
                obs.inc("reviewed_reply_lookup_total", labels={"result": "hit"})
                obs.inc("reviewed_reply_used_total", labels={"scope": row.get("match_scope") or "unknown"})
            return await fetch_one("SELECT * FROM fazle_reviewed_replies WHERE id = $1", row["id"])

    if touch:
        obs.inc("reviewed_reply_lookup_total", labels={"result": "miss"})
    return None


async def list_reviewed_replies(
    *,
    limit: int = 50,
    status: str = _DEFAULT_STATUS,
    intent: str = "",
    role: str = "",
    phone: str = "",
) -> list[dict[str, Any]]:
    rows = await fetch_all(
        """
        SELECT *
        FROM fazle_reviewed_replies
        WHERE ($1 = '' OR status = $1)
          AND ($2 = '' OR intent = $2)
          AND ($3 = '' OR COALESCE(role, '') = $3)
          AND ($4 = '' OR COALESCE(last10_phone, '') = $4)
        ORDER BY updated_at DESC, id DESC
        LIMIT $5
        """,
        (status or "").strip().lower(),
        (intent or "").strip().lower(),
        (role or "").strip().lower(),
        phone_last10(phone or ""),
        limit,
    )
    return rows


async def disable_reviewed_reply(reviewed_reply_id: int, reason: str = "") -> Optional[dict[str, Any]]:
    meta_patch = {"disabled_reason": reason or "disabled", "guard_version": "b26"}
    await execute(
        """
        UPDATE fazle_reviewed_replies
        SET status = $1,
            updated_at = NOW(),
            meta = COALESCE(meta, '{}'::jsonb) || $2::jsonb
        WHERE id = $3
        """,
        _DISABLED_STATUS,
        json.dumps(meta_patch),
        reviewed_reply_id,
    )
    obs.inc("reviewed_reply_disabled_total")
    return await fetch_one("SELECT * FROM fazle_reviewed_replies WHERE id = $1", reviewed_reply_id)


async def reactivate_reviewed_reply(reviewed_reply_id: int) -> Optional[dict[str, Any]]:
    await execute(
        """
        UPDATE fazle_reviewed_replies
        SET status = $1,
            updated_at = NOW()
        WHERE id = $2
        """,
        _DEFAULT_STATUS,
        reviewed_reply_id,
    )
    return await fetch_one("SELECT * FROM fazle_reviewed_replies WHERE id = $1", reviewed_reply_id)


def build_draft_meta(reviewed_row: Optional[dict[str, Any]], base_meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    meta = dict(base_meta or {})
    if not reviewed_row:
        return meta
    meta["reviewed_reply_id"] = reviewed_row.get("id")
    meta["reviewed_match_scope"] = reviewed_row.get("match_scope") or "intent_role"
    meta["reviewed_reply_status"] = reviewed_row.get("status") or "active"
    return meta


async def _lookup_scope(*, intent: str, role: str, draft_type: str, last10_phone: str) -> Optional[dict[str, Any]]:
    if not intent or not role:
        return None

    clauses = ["intent = $1", "COALESCE(role, '') = $2", "status = 'active'"]
    args: list[Any] = [intent, role]
    if draft_type:
        clauses.append(f"draft_type = ${len(args) + 1}")
        args.append(draft_type)
    if last10_phone:
        clauses.append(f"COALESCE(last10_phone, '') = ${len(args) + 1}")
        args.append(last10_phone)
    sql = (
        "SELECT * FROM fazle_reviewed_replies WHERE "
        + " AND ".join(clauses)
        + " ORDER BY priority ASC, usage_count DESC, updated_at DESC LIMIT 1"
    )
    return await fetch_one(sql, *args)


def _extract_role(draft_row: dict[str, Any]) -> str:
    meta = draft_row.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return str(meta.get("role") or "").strip().lower()