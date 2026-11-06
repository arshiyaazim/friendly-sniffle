"""
Fazle Core — Admin NL: chat keyword search (Phase 1.2 / v1.1.0)

Searches `wbom_whatsapp_messages.message_body` (ILIKE) within optional date range.

Public:
    intent_search(text, admin_phone) -> reply str
    is_search_query(text) -> bool

Triggers:
    find "advance" in chats
    search "escort" last 30 days
    "salary" এর কথা চ্যাটে কোথায় আছে
    keyword: vessel last week
"""
from __future__ import annotations

import logging
import re

from app.database import fetch_all
from .date_parser import parse_date_range

log = logging.getLogger("fazle.admin_nl_search")

# Quoted "term" or term: word or `find/search WORD`
_QUOTED_RE = re.compile(r'["“]([^"”]{2,80})["”]')
_FIND_RE = re.compile(
    r"\b(?:find|search|grep|keyword|খোঁজ|খুঁজে|সার্চ)\b\s*[:\-]?\s*([^\s,;]{2,40})",
    re.IGNORECASE,
)

_SEARCH_TRIGGER_RE = re.compile(
    r"\b(find|search|grep|keyword|খোঁজ|খুঁজে|সার্চ)\b|[\"“][^\"”]{2,80}[\"”]",
    re.IGNORECASE,
)

# Guard: payment/advance + phone → should go to employee_totals, not chat search
_PAY_GUARD_RE = re.compile(
    r"\b(advance|অগ্রিম|টাকা|নিয়েছে|পেয়েছে|payment|পেমেন্ট)\b",
    re.IGNORECASE | re.UNICODE,
)
_PHONE_GUARD_RE = re.compile(r"(?:\+?88)?(?:01[3-9]\d{8})")


def _extract_keyword(text: str) -> str | None:
    m = _QUOTED_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _FIND_RE.search(text)
    if m:
        return m.group(1).strip().strip('"\'`')
    return None


def is_search_query(text: str) -> bool:
    if not _SEARCH_TRIGGER_RE.search(text):
        return False
    # Don't intercept employee payment/advance queries — those go to employee_totals
    if _PAY_GUARD_RE.search(text) and _PHONE_GUARD_RE.search(text):
        return False
    if _PAY_GUARD_RE.search(text) and re.search(r"\bনিয়েছে\b", text, re.UNICODE):
        return False
    return _extract_keyword(text) is not None


async def intent_search(text: str, admin_phone: str) -> str:
    kw = _extract_keyword(text)
    if not kw:
        return ('❌ keyword পাইনি। উদাহরণ: find "advance" in chats last 30 days')

    rng = parse_date_range(text, default_days=30)
    assert rng is not None
    start, end, label = rng

    rows = await fetch_all(
        """
        SELECT received_at, sender_number, direction, COALESCE(message_body, '') AS body
          FROM wbom_whatsapp_messages
         WHERE received_at >= $1 AND received_at < $2
           AND message_body ILIKE '%' || $3 || '%'
         ORDER BY received_at DESC
         LIMIT 100
        """,
        start, end, kw,
    )

    if not rows:
        return f"🔎 \"{kw}\" — {label} — কোনো ম্যাচ নেই।"

    lines = [f"🔎 \"{kw}\" · {label} · {len(rows)} ম্যাচ:\n"]
    for r in rows[:50]:  # cap visible
        ts = r["received_at"].strftime("%d %b %H:%M")
        arrow = "→" if r["direction"] == "outbound" else "←"
        body = (r["body"] or "").replace("\n", " ").strip()
        # Highlight context window around keyword
        idx = body.lower().find(kw.lower())
        if idx >= 0:
            a = max(0, idx - 30)
            b = min(len(body), idx + len(kw) + 60)
            snippet = ("…" if a > 0 else "") + body[a:b] + ("…" if b < len(body) else "")
        else:
            snippet = body[:120]
        sender = (r["sender_number"] or "-")[-11:]
        lines.append(f"{ts} {arrow} {sender}: {snippet}")
    if len(rows) > 50:
        lines.append(f"\n…(আরও {len(rows) - 50} টি ম্যাচ আছে)")
    return "\n".join(lines)
