"""
Mock Ollama server — mimics Ollama's /api/generate endpoint.

Start:
  uvicorn tests.mocks.ollama_mock:app --port 11434

Returns canned Bengali responses for known intents, otherwise returns a
generic fallback. This allows E2E and integration tests to run without
a real GPU or Ollama installation.
"""
from __future__ import annotations

from fastapi import FastAPI, Body

app = FastAPI(title="Mock Ollama Server")

# Map keywords to canned replies
_CANNED_REPLIES: dict[str, str] = {
    "অগ্রিম": "আপনার অগ্রিম অনুরোধ গ্রহণ করা হয়েছে। প্রশাসকের অনুমোদনের জন্য অপেক্ষা করুন।",
    "হাজির": "আপনার উপস্থিতি রেকর্ড করা হয়েছে।",
    "escort": "এসকর্ট অর্ডার গ্রহণ করা হয়েছে। বিস্তারিত প্রদান করুন।",
    "mv ": "জাহাজের তথ্য নোট করা হয়েছে।",
    "help": "আমি আপনাকে সাহায্য করতে পারি: উপস্থিতি, অগ্রিম, এসকর্ট অর্ডার।",
}
_DEFAULT_REPLY = "আপনার বার্তা পাওয়া গেছে। একটু অপেক্ষা করুন।"


@app.get("/health")
async def health():
    return {"status": "ok", "model": "mock-llama3"}


@app.post("/api/generate")
async def generate(body: dict = Body(...)):
    prompt: str = body.get("prompt", "").lower()
    for keyword, reply in _CANNED_REPLIES.items():
        if keyword in prompt:
            response_text = reply
            break
    else:
        response_text = _DEFAULT_REPLY

    return {
        "model": body.get("model", "llama3"),
        "response": response_text,
        "done": True,
        "context": [],
        "total_duration": 100_000_000,
        "load_duration": 10_000_000,
        "prompt_eval_count": len(prompt.split()),
        "eval_count": len(response_text.split()),
    }


@app.post("/api/embeddings")
async def embeddings(body: dict = Body(...)):
    """Return fixed-size dummy embeddings."""
    return {"embedding": [0.0] * 384}


@app.get("/api/tags")
async def tags():
    return {"models": [{"name": "llama3", "size": 4_700_000_000}]}
