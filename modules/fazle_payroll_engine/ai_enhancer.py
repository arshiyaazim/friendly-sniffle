"""
Fazle Payroll Engine — Ollama AI enhancement layer.

Called ONLY when parser confidence < 0.7 or message_type == 'other'
but the text looks like it might be a payment (has amount pattern).

Reuses the existing app.ollama semaphore-serialized client — never
creates a new Ollama connection. Falls back gracefully on timeout.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from app.config import get_settings
from app.ollama import _ollama_sem  # reuse the existing semaphore

log = logging.getLogger("fazle.fpe.ai")

_CLASSIFY_PROMPT = """\
You are a payment data extractor for a Bangladeshi security company payroll system.
Given the following WhatsApp message, extract payment details if present.

Message:
{message}

Reply ONLY with a JSON object (no explanation):
{{
  "is_payment": true/false,
  "employee_name": "<name or null>",
  "payout_phone": "<01XXXXXXXXX or null>",
  "payout_method": "<bkash|nagad|cash|rocket|null>",
  "amount": <number or null>,
  "confidence": <0.0-1.0>,
  "notes": "<brief reason>"
}}
"""

AI_TIMEOUT_S = 20.0
AI_CONFIDENCE_THRESHOLD = 0.7  # only call AI when parser confidence is below this


async def ai_enhance_parse(
    message_text: str,
    parser_confidence: float,
) -> Optional[dict]:
    """
    Ask Ollama to extract payment fields from ambiguous message text.
    Returns a dict with extracted fields, or None on failure/timeout.

    This function is a no-op if parser_confidence >= AI_CONFIDENCE_THRESHOLD.
    """
    if parser_confidence >= AI_CONFIDENCE_THRESHOLD:
        return None

    settings = get_settings()
    prompt = _CLASSIFY_PROMPT.format(message=message_text[:500])

    try:
        async with _ollama_sem:
            async with httpx.AsyncClient(timeout=AI_TIMEOUT_S) as client:
                resp = await client.post(
                    f"{settings.ollama_url}/api/generate",
                    json={
                        "model": settings.ollama_model,
                        "prompt": prompt,
                        "stream": False,
                        "format": "json",
                        "options": {"num_predict": 150, "temperature": 0.1},
                    },
                )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("response", "")

        # Extract JSON from response
        parsed = json.loads(raw)
        log.info("[fpe.ai] extracted=%s conf=%.2f", parsed.get("is_payment"), parsed.get("confidence", 0))
        return parsed

    except httpx.TimeoutException:
        log.warning("[fpe.ai] Ollama timeout for message=%.60s", message_text)
        return None
    except (json.JSONDecodeError, KeyError, Exception) as exc:
        log.warning("[fpe.ai] AI enhance failed: %s", exc)
        return None
