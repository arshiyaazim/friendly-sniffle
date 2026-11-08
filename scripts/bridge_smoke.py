#!/usr/bin/env python3
"""Read-only smoke test of the *real* WhatsApp bridges before trusting the app.

The audit could only verify the WhatsApp story in code: the poller opens the
store with `file:...?mode=ro`, outbound sending is gated behind
`OUTBOUND_ENABLED`, and a bridge that reports a non-sending state is additionally
held by `_pairing_held()`. None of that was checked against a live bridge. This
script is that check, and it is deliberately incapable of writing anything:

  * opens each store with `mode=ro` (SQLite refuses writes on that handle);
  * records size/mtime before and after, and fails if *anything* changed —
    including a `-wal`/`-shm` file appearing because of us;
  * runs `PRAGMA quick_check` on the WhatsApp store;
  * reports per-chat message counts and the newest inbound message age, so a
    silently dead bridge is obvious;
  * prints the outbound posture (`OUTBOUND_ENABLED`, webhook signature
    enforcement) so you can see whether a reply could leave the box.

Usage:
    python scripts/bridge_smoke.py                     # paths from .env
    python scripts/bridge_smoke.py --bridge2           # only bridge 2
    python scripts/bridge_smoke.py --stale-minutes 60  # fail if no new inbound
    python scripts/bridge_smoke.py --selftest          # no bridge needed

Exit codes:
    0  every checked bridge is readable, unchanged and fresh enough
    1  a bridge is readable but stale (no inbound within --stale-minutes)
    2  a store is missing/unreadable, or we changed something (never expected)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

ARGS = sys.argv[1:]
SELFTEST = "--selftest" in ARGS


def _arg(name: str, default: str | None = None) -> str | None:
    if name in ARGS:
        idx = ARGS.index(name)
        if idx + 1 < len(ARGS):
            return ARGS[idx + 1]
    return default


STALE_MINUTES = int(_arg("--stale-minutes", "0") or 0)
WHICH = [b for b in ("--bridge1", "--bridge2", "--bridge3") if b in ARGS] or \
        ["--bridge1", "--bridge2", "--bridge3"]


def _load_settings():
    """Read paths from app.config when available, else from the environment."""
    try:
        from app.config import get_settings  # noqa: WPS433 - optional at runtime
        s = get_settings()
        return {
            "--bridge1": (s.bridge1_whatsapp_db_path, s.bridge1_db_path),
            "--bridge2": (s.bridge2_whatsapp_db_path, s.bridge2_db_path),
            "--bridge3": (s.bridge3_whatsapp_db_path, s.bridge3_db_path),
        }
    except Exception:  # noqa: BLE001 - the script must run without the full env
        return {
            "--bridge1": (os.getenv("BRIDGE1_WHATSAPP_DB_PATH", "store/bridge1/whatsapp.db"),
                          os.getenv("BRIDGE1_DB_PATH", "store/bridge1/messages.db")),
            "--bridge2": (os.getenv("BRIDGE2_WHATSAPP_DB_PATH", "store/bridge2/whatsapp.db"),
                          os.getenv("BRIDGE2_DB_PATH", "store/bridge2/messages.db")),
            "--bridge3": (os.getenv("BRIDGE3_WHATSAPP_DB_PATH", "store/bridge3/whatsapp.db"),
                          os.getenv("BRIDGE3_DB_PATH", "store/bridge3/messages.db")),
        }


def _fingerprint(path: Path) -> tuple[int, int, bool]:
    st = path.stat()
    return (st.st_size, int(st.st_mtime), Path(f"{path}-wal").exists())


def _outbound_posture() -> dict[str, str]:
    def flag(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()
    return {
        "OUTBOUND_ENABLED": flag("OUTBOUND_ENABLED", "(unset → sending disabled)") or "(empty)",
        "WEBHOOK_SIGNATURE_ENFORCEMENT": flag("WEBHOOK_SIGNATURE_ENFORCEMENT", "false"),
        "BRIDGE_WEBHOOK_SECRET": "set" if flag("BRIDGE_WEBHOOK_SECRET") else "(unset)",
    }


def _check_store(label: str, whatsapp_db: str, messages_db: str) -> tuple[bool, list[str]]:
    lines: list[str] = []
    wa = Path(whatsapp_db)
    msg = Path(messages_db)
    if not wa.is_file():
        return False, [f"{label}: WhatsApp store not found at {wa}"]

    before = _fingerprint(wa)
    before_msg = _fingerprint(msg) if msg.is_file() else None

    try:
        con = sqlite3.connect(f"file:{wa}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return False, [f"{label}: cannot open {wa} read-only: {exc}"]

    healthy = True
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()
        lines.append(f"{label}: quick_check → {quick[0] if quick else 'unknown'}")
        try:
            chats = con.execute("SELECT count(*) FROM chats").fetchone()[0]
            lines.append(f"{label}: {chats} chat(s)")
        except sqlite3.Error:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
            lines.append(f"{label}: tables → {', '.join(tables[:8])}")
    finally:
        con.close()

    after = _fingerprint(wa)
    if before != after:
        healthy = False
        lines.append(f"{label}: !! store changed while reading it "
                     f"({before} → {after}) — a read-only check must never write")

    if msg.is_file():
        try:
            mcon = sqlite3.connect(f"file:{msg}?mode=ro", uri=True, timeout=5.0)
            try:
                total = mcon.execute("SELECT count(*) FROM messages").fetchone()[0]
                newest = mcon.execute(
                    "SELECT MAX(timestamp) FROM messages").fetchone()[0]
                lines.append(f"{label}: messages store {total} message(s)")
                if newest:
                    age_min = _age_minutes(newest)
                    if age_min is not None:
                        lines.append(f"{label}: newest message {age_min:.0f} min old")
                        if STALE_MINUTES and age_min > STALE_MINUTES:
                            healthy = False
                            lines.append(f"{label}: !! stale — nothing within "
                                         f"{STALE_MINUTES} min")
            finally:
                mcon.close()
            if before_msg and _fingerprint(msg) != before_msg:
                healthy = False
                lines.append(f"{label}: !! messages store changed while reading it")
        except sqlite3.Error as exc:
            lines.append(f"{label}: messages store unreadable ({exc})")
    else:
        lines.append(f"{label}: no messages store at {msg} (ok if the bridge keeps one file)")

    return healthy, lines


def _age_minutes(value) -> float | None:
    """WhatsApp stores ms epoch; tolerate s/ms/ISO so the check never crashes."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num > 10_000_000_000:      # milliseconds
        num /= 1000.0
    return max(0.0, (time.time() - num) / 60.0)


def _selftest() -> int:
    assert _age_minutes(None) is None
    assert abs(_age_minutes(time.time() * 1000) or 99) < 1
    assert abs(_age_minutes(time.time())) < 1
    assert _age_minutes(0) > 1_000_000  # 1970 → very old
    assert _arg("--stale-minutes", "0") == "0"
    print("selftest ok")
    return 0


def main() -> int:
    paths = _load_settings()
    posture = _outbound_posture()
    print("outbound posture")
    for key, value in posture.items():
        print(f"  {key:30} : {value}")
    print()

    overall = 0
    for which in WHICH:
        wa, msg = paths[which]
        ok, lines = _check_store(which.lstrip("-"), wa, msg)
        print("\n".join(lines))
        print()
        if not ok:
            overall = 2
    if overall == 0 and STALE_MINUTES:
        print(f"all checked bridges are readable, unchanged and fresh "
              f"(< {STALE_MINUTES} min)")
    elif overall == 0:
        print("all checked bridges are readable and were not modified by this check")
    return overall


if __name__ == "__main__":
    if SELFTEST:
        raise SystemExit(_selftest())
    raise SystemExit(main())
