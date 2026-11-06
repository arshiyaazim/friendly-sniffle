"""
Fazle Core — Batch 22
Lightweight in-process observability: counters, histograms, and Prometheus
text-format exposition. Zero external dependencies.

Usage:
    from modules import observability as obs
    obs.inc("kb_hits_total", labels={"source": "db"})
    obs.observe("rag_search_ms", 12.4)

Special metric `http_requests_total` and `http_request_duration_ms` are
populated by the FastAPI middleware (see app/main.py).
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from collections import defaultdict
from threading import Lock
from typing import Any, Iterable, Optional

PROCESS_START = time.time()

# label tuple = sorted ((k,v), ...) so it's hashable
LabelTuple = tuple[tuple[str, str], ...]


def _label_tuple(labels: Optional[dict[str, str]]) -> LabelTuple:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


# ── Storage ───────────────────────────────────────────────────────────────────
_lock = Lock()
_counters: dict[str, dict[LabelTuple, float]] = defaultdict(lambda: defaultdict(float))
_gauges: dict[str, dict[LabelTuple, float]] = defaultdict(lambda: defaultdict(float))
# histogram buckets in ms (suitable for HTTP latency + module timings)
_DEFAULT_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
_histograms: dict[str, dict[LabelTuple, dict[str, Any]]] = defaultdict(
    lambda: defaultdict(lambda: {"count": 0, "sum": 0.0, "buckets": [0] * len(_DEFAULT_BUCKETS_MS)})
)


# ── Public API ────────────────────────────────────────────────────────────────
def inc(name: str, value: float = 1.0, labels: Optional[dict[str, str]] = None) -> None:
    lt = _label_tuple(labels)
    with _lock:
        _counters[name][lt] += value


def gauge(name: str, value: float, labels: Optional[dict[str, str]] = None) -> None:
    lt = _label_tuple(labels)
    with _lock:
        _gauges[name][lt] = value


def observe(name: str, value_ms: float, labels: Optional[dict[str, str]] = None) -> None:
    """Record an observation in milliseconds into a histogram.
    Buckets are stored as per-bucket (non-cumulative) counts;
    Prometheus output accumulates them at render time."""
    lt = _label_tuple(labels)
    with _lock:
        h = _histograms[name][lt]
        h["count"] += 1
        h["sum"] += value_ms
        # Increment exactly one bucket: smallest le that fits, else overflow into +Inf via count
        for i, b in enumerate(_DEFAULT_BUCKETS_MS):
            if value_ms <= b:
                h["buckets"][i] += 1
                break


# ── Snapshot helpers ──────────────────────────────────────────────────────────
def snapshot() -> dict[str, Any]:
    """Return a JSON-friendly snapshot of all metrics."""
    with _lock:
        return {
            "uptime_s": round(time.time() - PROCESS_START, 1),
            "counters": {
                name: [{"labels": dict(lt), "value": v} for lt, v in series.items()]
                for name, series in _counters.items()
            },
            "gauges": {
                name: [{"labels": dict(lt), "value": v} for lt, v in series.items()]
                for name, series in _gauges.items()
            },
            "histograms": {
                name: [
                    {
                        "labels": dict(lt),
                        "count": h["count"],
                        "sum_ms": round(h["sum"], 3),
                        "avg_ms": round(h["sum"] / h["count"], 3) if h["count"] else 0,
                        "buckets_ms": dict(zip([str(b) for b in _DEFAULT_BUCKETS_MS], h["buckets"])),
                    }
                    for lt, h in series.items()
                ]
                for name, series in _histograms.items()
            },
        }


# ── Prometheus text exposition ────────────────────────────────────────────────
def _fmt_labels(lt: LabelTuple) -> str:
    if not lt:
        return ""
    parts = [f'{k}="{_escape(v)}"' for k, v in lt]
    return "{" + ",".join(parts) + "}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus() -> str:
    out: list[str] = []
    with _lock:
        # uptime gauge (always present)
        out.append("# TYPE fazle_uptime_seconds gauge")
        out.append(f"fazle_uptime_seconds {time.time() - PROCESS_START:.1f}")

        for name, series in _counters.items():
            out.append(f"# TYPE {name} counter")
            for lt, v in series.items():
                out.append(f"{name}{_fmt_labels(lt)} {v}")
        for name, series in _gauges.items():
            out.append(f"# TYPE {name} gauge")
            for lt, v in series.items():
                out.append(f"{name}{_fmt_labels(lt)} {v}")
        for name, series in _histograms.items():
            out.append(f"# TYPE {name} histogram")
            for lt, h in series.items():
                cum = 0
                base = _fmt_labels(lt)[1:-1] if lt else ""
                for i, b in enumerate(_DEFAULT_BUCKETS_MS):
                    cum += h["buckets"][i]
                    le_lbl = (base + "," if base else "") + f'le="{b}"'
                    out.append(f'{name}_bucket{{{le_lbl}}} {cum}')
                # +Inf bucket = total count
                inf_lbl = (base + "," if base else "") + 'le="+Inf"'
                out.append(f'{name}_bucket{{{inf_lbl}}} {h["count"]}')
                out.append(f"{name}_sum{_fmt_labels(lt)} {h['sum']:.3f}")
                out.append(f"{name}_count{_fmt_labels(lt)} {h['count']}")
    out.append("")
    return "\n".join(out)


# ── Test/reset helper ─────────────────────────────────────────────────────────
def _reset_for_tests() -> None:
    with _lock:
        _counters.clear()
        _gauges.clear()
        _histograms.clear()
