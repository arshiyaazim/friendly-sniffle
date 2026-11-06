"""Risk rules for deciding whether auto-send is allowed."""
from __future__ import annotations

from .classifier import Classification

RISKY_INTENTS = {
    "accountant",
    "transaction",
    "employee_id",
    "reports_issue",
    "escort_order",
    "escort_client",
    "roster_issue",
    "internal_operations",
    "complaint",
    "payment_issue",
    "legal_issue",
    "scam_allegation",
    "abuse",
    "negative_comment",
    "media_only",
    "unclear",
}

SAFE_AUTO_SEND_INTENTS = {
    "greeting",
    "interested",
    "job_details",
    "salary",
    "salary_objection",
    "location",
    "age_issue",
    "documents",
    "fees",
    "applicant_info_complete",
    "training",
    "join_process",
    "recruitment_follow_up",
    "career_growth",
    "accommodation",
}

RECRUITING_INTENTS = SAFE_AUTO_SEND_INTENTS


def risk_reason(classification: Classification, *, media_flag: bool = False, text: str = "") -> str | None:
    if media_flag and not (text or "").strip():
        return "media_only"
    if classification.intent in RISKY_INTENTS:
        return classification.intent
    return None


def can_auto_send(classification: Classification, *, platform: str, text: str = "") -> bool:
    if risk_reason(classification, text=text):
        return False
    if platform in {"messenger", "meta_whatsapp", "facebook_comment"}:
        return classification.intent in SAFE_AUTO_SEND_INTENTS
    return False


def is_recruiting_intent(intent: str) -> bool:
    return intent in RECRUITING_INTENTS


ESCALATION_INTENTS = {
    "employee_salary_complaint",
    "legal_issue",
    "payment_issue",
}


def is_escalation_intent(intent: str) -> bool:
    return intent in ESCALATION_INTENTS
