"""Idempotency helpers for social event and reply processing."""
from __future__ import annotations

import hashlib


def event_key(*, platform: str, event_type: str, external_id: str | None, sender_id: str | None, text: str | None) -> str:
    if external_id:
        raw = f"{platform}|{event_type}|{external_id}"
    else:
        raw = f"{platform}|{event_type}|{sender_id or ''}|{(text or '')[:300]}"
    return hashlib.sha256(raw.encode()).hexdigest()


def reply_key(*, platform: str, target_id: str, event_id: int | None, reply_text: str) -> str:
    raw = f"{platform}|{target_id}|{event_id or ''}|{reply_text[:300]}"
    return hashlib.sha256(raw.encode()).hexdigest()
