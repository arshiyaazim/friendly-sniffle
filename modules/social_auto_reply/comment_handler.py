"""Facebook comment event handling."""
from __future__ import annotations

from .classifier import classify_comment
from .reply_generator import generate_comment_reply
from .risk_flagger import can_auto_send, risk_reason


def build_comment_decision(text: str) -> dict:
    classification = classify_comment(text)
    reason = risk_reason(classification, text=text)
    if reason:
        return {"classification": classification, "reply": "", "flag_reason": reason}
    if not can_auto_send(classification, platform="facebook_comment", text=text):
        return {"classification": classification, "reply": "", "flag_reason": "not_auto_sendable"}
    return {"classification": classification, "reply": generate_comment_reply(classification, text=text), "flag_reason": None}
