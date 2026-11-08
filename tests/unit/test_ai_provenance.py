"""Oudbound AI provenance (2026-11-06) — modules.ai_provenance.

Answers the owner question "if a message was AI-generated, can we tell?" with
tests for the exact decision table the message writers rely on:

  * a fresh successful generation  -> ai_generated=True + provider/model/task
  * actor_type='human_device'      -> False, regardless of context
  * nothing recorded                -> False (rule/template), never guessed True
  * stale generation                -> ignored (freshness window)
  * non-content tasks (intent classification, admin RAG, internal decision)
                                    -> never recorded at all
"""
from __future__ import annotations

import time

import pytest

from modules import ai_provenance


@pytest.fixture(autouse=True)
def _clean():
    ai_provenance.reset_for_test()
    yield
    ai_provenance.reset_for_test()


class TestRecordAndResolve:
    def test_success_is_exposed_with_provider_and_model(self):
        ai_provenance.set_task("reply")
        ai_provenance.record_success("ollama", "hermes3:3b")
        prov = ai_provenance.current()
        assert prov is not None
        assert prov["provider"] == "ollama"
        assert prov["model"] == "hermes3:3b"
        assert prov["task"] == "reply"

    def test_resolve_marks_ai_generated_true_with_provenance(self):
        ai_provenance.set_task("recruitment_reply")
        ai_provenance.record_success("groq", "allam-2-7b")
        fields = ai_provenance.resolve(actor_type="automation")
        assert fields == {
            "ai_generated": True,
            "ai_provider": "groq",
            "ai_model": "allam-2-7b",
            "ai_task": "recruitment_reply",
        }

    def test_human_device_always_wins(self):
        ai_provenance.set_task("reply")
        ai_provenance.record_success("ollama", "hermes3:3b")
        fields = ai_provenance.resolve(actor_type="human_device")
        assert fields["ai_generated"] is False
        assert fields["ai_provider"] is None

    def test_no_provenance_is_false_not_guess(self):
        fields = ai_provenance.resolve(actor_type="automation")
        assert fields["ai_generated"] is False
        assert fields["ai_provider"] is None

    def test_explicit_true_without_context_is_kept(self):
        fields = ai_provenance.resolve(actor_type="automation", ai_generated=True,
                                       provider="minimax", model="m3")
        assert fields["ai_generated"] is True
        assert fields["ai_provider"] == "minimax"


class TestFreshnessAndTaskFilter:
    def test_stale_generation_is_ignored(self):
        ai_provenance.set_task("reply")
        ai_provenance.record_success("ollama", "hermes3:3b")
        ai_provenance._CURRENT.set({**ai_provenance.current(), "at": time.time() - 10_000})
        assert ai_provenance.current() is None
        assert ai_provenance.resolve(actor_type="automation")["ai_generated"] is False

    @pytest.mark.parametrize("task", ["classify_intent", "decision", "rag_answer"])
    def test_non_content_tasks_are_not_recorded(self, task):
        ai_provenance.set_task(task)
        ai_provenance.record_success("ollama", "hermes3:3b")
        assert ai_provenance.current() is None

    @pytest.mark.parametrize("task", ["reply", "recruitment_reply", "chat_reply"])
    def test_content_tasks_are_recorded(self, task):
        ai_provenance.set_task(task)
        ai_provenance.record_success("groq", "allam-2-7b")
        assert ai_provenance.current()["task"] == task

    def test_unlabelled_direct_provider_call_is_recorded(self):
        # app/ollama.generate_reply() can be called directly (route_b, KB),
        # outside app.llm's task labelling — those must still be markable.
        ai_provenance.record_success("ollama", "hermes3:3b", task="reply")
        assert ai_provenance.current()["provider"] == "ollama"

    def test_blank_provider_is_ignored(self):
        ai_provenance.record_success("", "whatever")
        assert ai_provenance.current() is None

    def test_provenance_meta_is_enqueue_snapshot(self):
        ai_provenance.set_task("reply")
        ai_provenance.record_success("omniroute", "auto")
        meta = ai_provenance.provenance_meta()
        assert meta["ai_generated"] is True
        assert meta["ai_provider"] == "omniroute"
        assert meta["ai_model"] == "auto"

    def test_provenance_meta_empty_without_generation(self):
        assert ai_provenance.provenance_meta() == {}
