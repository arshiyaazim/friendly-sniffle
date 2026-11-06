"""Payment issue handling for social channels."""
from __future__ import annotations

from . import reply_rules as rules


def initial_payment_reply() -> str:
    return rules.PAYMENT_INFO_REQUEST_REPLY


def escalation_reply() -> str:
    return rules.PAYMENT_ESCALATION_REPLY
