#!/usr/bin/env python3
"""Restart gate — readiness classification (Check 9 of the deploy gate).

A restart is not "done" when `systemctl restart` returns: the process can come
back into a crash loop, or come up serving a broken core probe while systemd is
perfectly happy. This module answers one question, after a restart:

    is the service actually READY, and if not, exactly why?

Usage
-----
    python -m scripts.restart_gate_readiness \
        --service fazle-core \
        --url http://127.0.0.1:8200/health \
        --err-log logs/fazle-core.error.log \
        --restarts-before "$BEFORE" \
        --timeout 90

    BEFORE=$(systemctl show -p NRestarts --value fazle-core)
    systemctl restart fazle-core
    python -m scripts.restart_gate_readiness --restarts-before "$BEFORE"

Exit codes: 0 = ready, 1 = not ready (reason on stderr), 2 = usage/setup error.

Design notes
------------
* **The ambient/core split is the point of this file.** `/health` returns 503
  when *any* probe is bad, including host-level ones (cpu/disk/mem). A deploy
  is not a failure because the box is busy — those probes are reported as
  ``ambient`` and never block the gate on their own. Core probes (db, bridges,
  outbound queue, scheduler) do block it.
* **Both signals are required for success**: the log must show
  ``Application startup complete.`` *and* `/health` must classify as ready. A
  service can answer /health from a half-initialised state, and can log
  startup-complete and then die.
* **An unhandled traceback always loses the gate**, even if the same log window
  also contains startup-complete — never report success on a log tail that
  shows an exception.
* **No app/modules imports.** This runs on the host, outside the venv, against
  a service that may be in any state; it must not be able to fail because the
  app it is checking is broken. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

# Probes that mean "this deployment is not working". Anything here that is not
# `ok` fails the gate (after the settle window).
CORE_PROBES = (
    "db",
    "bridge1_db",
    "bridge2_db",
    "bridge3_db",
    "bridge_poller_b1",
    "bridge_poller_b2",
    "bridge_poller_b3",
    "outbound",
    "scheduler",
)

# Probes that describe the HOST, not the deploy. Reported, never blocking.
AMBIENT_PROBES = ("cpu", "disk", "mem")

# A probe in one of these states is not a failure. `skipped` is what the deep
# health path reports for probes it did not run (e.g. llm on a shallow check).
_OK_PROBE_STATES = frozenset({"ok", "skipped", "unknown"})

# The unit writes uvicorn's stderr here (fazle-core.service, StandardError=),
# and the gate reads the same file. Keep the two in sync — a mismatch makes
# every restart look like "no startup signal".
DEFAULT_ERR_LOG = "logs/fazle-core.error.log"
DEFAULT_SERVICE = "fazle-core"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8200/health"

_STARTUP_COMPLETE = "Application startup complete."
_TRACEBACK_LINE = "Traceback (most recent call last)"
# "pydantic_core...ValidationError: 1 validation error", "RuntimeError: boom"
_EXCEPTION_LINE_RE = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failure)\s*:"
)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ── Pure classification (unit-tested directly) ───────────────────────────────

def classify_process_state(active_state: str,
                           restarts_before: int,
                           restarts_now: int) -> Optional[str]:
    """Return a failure reason, or None when the process state is acceptable.

    `active_state` is systemd's ActiveState; the restart counter is compared
    against the value read BEFORE the restart was issued, so a single expected
    transition (active, counter +1) is not mistaken for a crash loop.
    """
    state = str(active_state or "").strip().lower()
    before = _as_int(restarts_before)
    now = _as_int(restarts_now)

    if state == "failed":
        return "process entered 'failed' state (systemd gave up restarting it)"

    # The counter only matters while the unit is NOT settled in `active`:
    # a successful restart also bumps it, and that is the expected case.
    if now > before and state != "active":
        return (f"process crash-looped: restart counter advanced "
                f"{before} -> {now} while ActiveState was {state or 'unknown'!r}")
    return None


def scan_error_log_tail(tail: str) -> Optional[str]:
    """Classify a log tail: "EXCEPTION: ...", "STARTED", or None.

    Exceptions win over the startup signal on purpose (see module docstring).
    """
    if not tail or not tail.strip():
        return None

    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    if not lines:
        return None

    has_traceback = any(_TRACEBACK_LINE in line for line in lines)
    exception_line = ""
    for line in reversed(lines):
        if _EXCEPTION_LINE_RE.match(line):
            exception_line = line
            break

    if has_traceback or exception_line:
        return f"EXCEPTION: {exception_line or lines[-1]}"
    if _STARTUP_COMPLETE in tail:
        return "STARTED"
    return None


def classify_health(status_code: Optional[int], body: Any) -> dict:
    """Classify one /health response.

    Returns a dict with:
      state    -- "ready" | "core_bad" | "unreachable" | "malformed"
      bad_core -- {probe: status} for failing core probes
      ambient  -- {probe: status} for failing host-level probes

    `state == "ready"` deliberately ignores the HTTP status code and the
    aggregate ``status`` field: a 503 caused only by cpu/disk/mem is a healthy
    deploy on a busy host, which is the false negative this module exists to
    stop reporting as a failure.
    """
    if status_code is None:
        return {"state": "unreachable", "code": None, "bad_core": {}, "ambient": {}}
    if not isinstance(body, dict):
        return {"state": "malformed", "code": status_code, "bad_core": {}, "ambient": {}}

    probes = body.get("probes")
    if not isinstance(probes, dict):
        return {"state": "malformed", "code": status_code, "bad_core": {}, "ambient": {}}

    bad_core: dict = {}
    for name in CORE_PROBES:
        status = str((probes.get(name) or {}).get("status", "ok")).strip().lower()
        if status not in _OK_PROBE_STATES:
            bad_core[name] = status

    ambient: dict = {}
    for name in AMBIENT_PROBES:
        status = str((probes.get(name) or {}).get("status", "ok")).strip().lower()
        if status not in _OK_PROBE_STATES:
            ambient[name] = status

    return {
        "state": "core_bad" if bad_core else "ready",
        "code": status_code,
        "overall": body.get("status"),
        "bad_core": bad_core,
        "ambient": ambient,
    }


# ── I/O helpers (patched out in tests) ──────────────────────────────────────

def _systemctl_show(service: str, prop: str) -> str:
    """`systemctl show -p <prop> --value <service>`; "" on any failure."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", prop, "--value", service],
            capture_output=True, text=True, timeout=10,
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _read_new_tail(path: str, offset: int = 0, max_lines: int = 200) -> str:
    """Last `max_lines` lines of the error log; "" when it is not readable.

    A missing log is not itself a failure — the caller reports a missing
    startup signal, which is the actionable statement.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return ""
    start = offset if 0 <= offset < len(lines) else max(0, len(lines) - max_lines)
    return "".join(lines[start:][-max_lines:])


def _fetch_health(url: str, timeout: float = 5.0) -> tuple[Optional[int], Any]:
    """GET the health URL. Returns (status_code, body) or (None, None)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read()
            code = getattr(response, "status", None) or response.getcode()
    except urllib.error.HTTPError as exc:  # 503 carries a JSON body; keep it
        code = exc.code
        try:
            raw = exc.read()
        except Exception:
            return code, None
    except Exception:
        return None, None
    try:
        return code, json.loads(raw or b"{}")
    except Exception:
        return code, None


# ── The gate ────────────────────────────────────────────────────────────────

def poll(service: str, health_url: str, err_log: str,
         restarts_before: int = 0, offset: int = 0,
         timeout_s: float = 90, interval_s: float = 3) -> int:
    """Poll until ready or window expiry. Returns the process exit code."""
    started = time.time()
    deadline = started + max(1.0, float(timeout_s))
    startup_seen = False
    last_health: dict = {"state": "unknown"}

    while True:
        active_state = _systemctl_show(service, "ActiveState")
        restarts_now = _as_int(_systemctl_show(service, "NRestarts"))
        reason = classify_process_state(active_state, restarts_before, restarts_now)
        if reason:
            print(f"RESTART: FAILED — {reason}", file=sys.stderr)
            return 1

        signal = scan_error_log_tail(_read_new_tail(err_log, offset))
        if signal and signal.startswith("EXCEPTION:"):
            print(f"RESTART: FAILED — startup exception detected: "
                  f"{signal.split(':', 1)[1].strip()}", file=sys.stderr)
            return 1
        if signal == "STARTED":
            startup_seen = True

        status_code, body = _fetch_health(health_url)
        last_health = classify_health(status_code, body)

        if startup_seen and last_health["state"] == "ready":
            print(f"RESTART: SUCCESS — {service} is active and {health_url} is ready")
            if last_health.get("ambient"):
                print(f"  ambient (host-level, not a deploy failure): "
                      f"{last_health['ambient']}")
            if last_health.get("overall"):
                print(f"  overall={last_health['overall']} "
                      f"http={last_health.get('code')}")
            return 0

        if time.time() >= deadline:
            break
        time.sleep(interval_s)

    if not startup_seen:
        print(f"RESTART: FAILED — no 'Application startup complete.' signal in "
              f"{err_log} within {timeout_s:g}s", file=sys.stderr)
    elif last_health["state"] == "core_bad":
        detail = ", ".join(f"{name}={state}"
                           for name, state in sorted(last_health["bad_core"].items()))
        print(f"RESTART: FAILED — core application probe(s) unhealthy: {detail}",
              file=sys.stderr)
    elif last_health["state"] == "unreachable":
        print(f"RESTART: FAILED — {health_url} unreachable for the whole "
              f"{timeout_s:g}s window", file=sys.stderr)
    else:
        print(f"RESTART: FAILED — {health_url} returned an unusable body "
              f"(state={last_health['state']}, http={last_health.get('code')})",
              file=sys.stderr)
    return 1


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restart gate: is the service actually ready after a restart?")
    parser.add_argument("--service", default=DEFAULT_SERVICE,
                        help="systemd unit name (default: fazle-core)")
    parser.add_argument("--url", default=DEFAULT_HEALTH_URL,
                        help="health endpoint (default: %(default)s)")
    parser.add_argument("--err-log", default=DEFAULT_ERR_LOG,
                        help="stderr log the unit appends to (default: %(default)s)")
    parser.add_argument("--restarts-before", type=int, default=None,
                        help="NRestarts read BEFORE the restart; defaults to the "
                             "current value (which disables crash-loop detection)")
    parser.add_argument("--offset", type=int, default=0,
                        help="line offset into the error log to start scanning from")
    parser.add_argument("--timeout", type=float, default=90,
                        help="total settle window in seconds (default: %(default)s)")
    parser.add_argument("--interval", type=float, default=3,
                        help="seconds between polls (default: %(default)s)")
    args = parser.parse_args(argv)

    before = args.restarts_before
    if before is None:
        before = _as_int(_systemctl_show(args.service, "NRestarts"))

    return poll(args.service, args.url, args.err_log, before, args.offset,
                args.timeout, args.interval)


if __name__ == "__main__":
    sys.exit(main())
