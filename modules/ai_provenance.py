"""AI provenance — "was this message written by a model, and which one?"
===========================================================================

Owner question this answers (2026-11-06): *"যদি এআই জেনারেটেড ম্যাসেজ হয় তাহলে
তা বোঝার কোনো উপায় কি আছে?"* — before this module the answer was no: an
outbound body stored in `wbom_whatsapp_messages` looked identical whether a
human typed it on the phone, a rule/template produced it, or ollama/groq/
omniroute/kimi wrote it.

How it works
------------
`app.llm._record_attempt()` already records every provider attempt as
(provider, model, outcome) for the CURRENT asyncio task. On `outcome ==
"success"` it now also calls :func:`record_success` here, which stashes the
provider/model/timestamp in a ContextVar — the same isolation the existing
attempt trace uses, so two concurrent conversations can never see each
other's provenance.

Consumers:
  * `modules.outbound.enqueue()` snapshots it into
    `fazle_outbound_queue.meta_json` (durable: the queue row outlives the
    task, and the send happens later in the dispatcher's own task).
  * `modules.outbound.sweep_once()` copies it onto the canonical outbound row
    it writes at send-success.
  * `modules.bridge_poller._save_message()` / `persist_outbound_message()`
    read it for rows written immediately after a generation (direct-reply
    cascades, admin-Hermes replies).

Honesty rules (kept deliberately conservative):
  * Freshness window: a success older than `FRESH_S` is NOT used. A message
    saved long after a generation must not inherit that generation's provider.
  * `actor_type='human_device'` always wins: a message typed on the linked
    phone is never labelled AI, whatever the context says.
  * Absent provenance => `ai_generated=False` for live rows (a deterministic
    rule/template produced it), and `None`/NULL for history imports where the
    origin cannot be proven at all. We never guess "true".
"""
from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Optional

log = logging.getLogger("fazle.ai_provenance")

# A generation is normally enqueued/saved within seconds. Fifteen minutes is
# generous enough for a draft→approve→send hop and short enough that a stale
# marker cannot leak onto an unrelated message in the same long-lived task.
FRESH_S = 15 * 60

_CURRENT: ContextVar[Optional[dict]] = ContextVar("fazle_ai_provenance_current", default=None)
_TASK: ContextVar[Optional[str]] = ContextVar("fazle_ai_provenance_task", default=None)

# Only these task labels mean "a model wrote a customer-facing body". Intent
# classification, admin RAG answers and internal decision calls are excluded:
# their success must never label a neighbouring rule-based reply as AI-written.
CONTENT_TASKS = frozenset({"reply", "recruitment_reply", "chat_reply"})


def set_task(task: str) -> None:
    """Label the generation the current task is about to attempt.

    Purely descriptive (`reply`, `recruitment_reply`, `classify_intent`,
    `chat_reply`, `rag_answer`, `decision`) — stored in
    `wbom_whatsapp_messages.ai_task` so a later audit can tell an intent
    classification from a customer-facing reply.
    """
    _TASK.set((task or "").strip() or None)


def record_success(provider: str, model: str, task: Optional[str] = None) -> None:
    """Called by app.llm (and the direct provider modules) on success.

    Ignored when the current task label is known NOT to produce message text
    (intent classification, admin RAG, internal decision calls) — see
    CONTENT_TASKS above.
    """
    provider = (provider or "").strip()
    if not provider:
        return
    label = (task or _TASK.get() or "").strip() or None
    if label is not None and label not in CONTENT_TASKS:
        return
    _CURRENT.set({
        "provider": provider,
        "model": (model or "").strip() or None,
        "task": label,
        "at": time.time(),
    })


def clear() -> None:
    _CURRENT.set(None)
    _TASK.set(None)


def current(max_age_s: int = FRESH_S) -> Optional[dict]:
    """The generation that produced the text being saved, if it is fresh."""
    prov = _CURRENT.get()
    if not prov:
        return None
    if (time.time() - float(prov.get("at") or 0)) > max_age_s:
        return None
    return dict(prov)


def resolve(
    *,
    actor_type: str = "",
    ai_generated: Optional[bool] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    task: Optional[str] = None,
    max_age_s: int = FRESH_S,
) -> dict:
    """Decide the (ai_generated, provider, model, task) tuple to persist.

    `ai_generated` may be passed explicitly by a caller that knows the answer
    (e.g. the queue dispatcher replaying meta_json, or the history importer
    writing NULL for "unknown"). When it is None the decision is:

      * actor_type == 'human_device'  -> False, no provider (typed on phone)
      * fresh generation in context   -> True + provider/model/task
      * otherwise                     -> False (deterministic rule/template)
    """
    prov = current(max_age_s=max_age_s)
    if ai_generated is None:
        if (actor_type or "").strip() == "human_device":
            ai_generated = False
        elif prov:
            ai_generated = True
        else:
            ai_generated = False
    if ai_generated and not provider and prov:
        provider = prov.get("provider")
        model = model or prov.get("model")
        task = task or prov.get("task")
    return {
        "ai_generated": ai_generated,
        "ai_provider": provider or None,
        "ai_model": model or None,
        "ai_task": task or None,
    }


def provenance_meta(max_age_s: int = FRESH_S) -> dict:
    """Snapshot for `fazle_outbound_queue.meta_json` (empty dict when none)."""
    prov = current(max_age_s=max_age_s)
    if not prov:
        return {}
    return {
        "ai_generated": True,
        "ai_provider": prov.get("provider"),
        "ai_model": prov.get("model"),
        "ai_task": prov.get("task"),
    }


def reset_for_test() -> None:
    clear()
