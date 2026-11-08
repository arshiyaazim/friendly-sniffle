"""CLI: import a bridge store's existing history.

Usage
-----
    python -m modules.history_backfill                      # every bridge
    python -m modules.history_backfill --bridge bridge2
    python -m modules.history_backfill --bridge bridge1 --since 2025-01-01
    python -m modules.history_backfill --inbound-only
    python -m modules.history_backfill --dry-run            # count only
    python -m modules.history_backfill --reset --bridge bridge3

Never routes, replies, or triggers anything — see the module docstring.
Exit code is 1 when any bridge reports a failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone


def _parse_date(value: str | None):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _main_async(args) -> int:
    from modules.history_backfill import backfill_bridge, backfill_all

    since = _parse_date(args.since)
    until = _parse_date(args.until)
    common = {
        "since": since,
        "until": until,
        "include_outbound": not args.inbound_only,
        "include_groups": args.include_groups,
        "max_messages": args.max_messages,
        "reset": args.reset,
        "dry_run": args.dry_run,
    }
    if args.bridge == "all":
        results = await backfill_all(
            include_outbound=common["include_outbound"],
            include_groups=common["include_groups"],
            max_messages_per_bridge=args.max_messages,
            reset=args.reset,
        )
    else:
        results = [await backfill_bridge(args.bridge, **common)]
    print(json.dumps(results, indent=2, default=str))
    return 0 if all(r.get("status") not in {"failed", "no_store"} for r in results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a WhatsApp bridge store's history into the main database.")
    ap.add_argument("--bridge", default="all", choices=["all", "bridge1", "bridge2", "bridge3"])
    ap.add_argument("--since", default=None, help="ISO timestamp lower bound (default: whole store)")
    ap.add_argument("--until", default=None, help="ISO timestamp upper bound")
    ap.add_argument("--inbound-only", action="store_true", help="skip is_from_me=1 messages")
    ap.add_argument("--include-groups", action="store_true", help="include APPROVED group JIDs only")
    ap.add_argument("--max-messages", type=int, default=None, help="pause after N messages (resumable)")
    ap.add_argument("--reset", action="store_true", help="re-import from the beginning")
    ap.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    return asyncio.run(_main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
