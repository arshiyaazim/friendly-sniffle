"""OpenAI-compatible provider adapter for administrator-configured AI routes."""
from __future__ import annotations

import asyncio

from openai import AsyncOpenAI

from shared.reply_policy import (
    build_whatsapp_recruitment_policy,
    build_whatsapp_reply_policy,
)

_provider_sem = asyncio.Semaphore(3)
_VALID_INTENTS = {
    "recruitment", "salary_query", "payment_due", "escort_duty", "complaint",
    "client_order", "leave", "join", "attendance", "slip_submission",
    "voice_note", "greeting", "unknown",
}


def _make_client(route_api_key: str | None, route_base_url: str) -> AsyncOpenAI:
    if not route_api_key:
        raise ValueError("provider credential is not configured")
    if not route_base_url:
        raise ValueError("provider base URL is not configured")
    return AsyncOpenAI(api_key=route_api_key, base_url=route_base_url)


async def _complete(
    *,
    messages: list[dict[str, str]],
    route_model: str,
    route_base_url: str,
    route_api_key: str | None,
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    if not route_model:
        raise ValueError("provider model is not configured")
    async with _provider_sem:
        async with _make_client(route_api_key, route_base_url) as client:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=route_model,
                    messages=messages,
                    max_tokens=max(1, min(max_tokens, 4096)),
                    temperature=max(0.0, min(temperature, 1.0)),
                ),
                timeout=timeout,
            )
    return (response.choices[0].message.content or "").strip()


async def classify_intent_llm(
    text: str,
    *,
    route_model: str,
    route_base_url: str,
    route_api_key: str | None,
    router_managed: bool = True,
) -> str:
    prompt = (
        "Classify this WhatsApp message into one category. Reply with ONLY the category name.\n\n"
        "Categories: recruitment, salary_query, payment_due, escort_duty, complaint, "
        "client_order, leave, join, attendance, slip_submission, voice_note, greeting, unknown\n\n"
        f"Message: {text[:300]}\n\nCategory:"
    )
    result = await _complete(
        messages=[{"role": "user", "content": prompt}],
        route_model=route_model,
        route_base_url=route_base_url,
        route_api_key=route_api_key,
        max_tokens=16,
        temperature=0.0,
        timeout=15.0,
    )
    category = result.lower().split()
    return category[0] if category and category[0] in _VALID_INTENTS else "unknown"


async def generate_reply(
    user_message: str,
    intent: str,
    db_context: str = "",
    history: str = "",
    role: str = "new_lead",
    source: str = "unknown",
    *,
    route_model: str,
    route_base_url: str,
    route_api_key: str | None,
    router_managed: bool = True,
) -> str | None:
    prompt = build_whatsapp_reply_policy(
        source=source,
        user_message=user_message,
        role=role,
        intent=intent,
        db_context=db_context,
        history=history,
    )
    return await _complete(
        messages=[{"role": "user", "content": prompt}],
        route_model=route_model,
        route_base_url=route_base_url,
        route_api_key=route_api_key,
        max_tokens=200,
        temperature=0.4,
        timeout=25.0,
    )


async def generate_recruitment_reply(
    user_message: str,
    kb_context: str,
    history: str = "",
    contact_context: str = "",
    source: str = "unknown",
    *,
    route_model: str,
    route_base_url: str,
    route_api_key: str | None,
    router_managed: bool = True,
) -> str | None:
    prompt = build_whatsapp_recruitment_policy(
        source=source,
        user_message=user_message,
        kb_context=kb_context,
        history=history,
        contact_context=contact_context,
    )
    return await _complete(
        messages=[{"role": "user", "content": prompt}],
        route_model=route_model,
        route_base_url=route_base_url,
        route_api_key=route_api_key,
        max_tokens=200,
        temperature=0.2,
        timeout=25.0,
    )


async def decision_call(
    *,
    system_prompt: str,
    user_payload: str,
    max_tokens: int = 300,
    temperature: float = 0.1,
    timeout: float = 12.0,
    route_model: str,
    route_base_url: str,
    route_api_key: str | None,
    router_managed: bool = True,
) -> str | None:
    return await _complete(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        route_model=route_model,
        route_base_url=route_base_url,
        route_api_key=route_api_key,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )
