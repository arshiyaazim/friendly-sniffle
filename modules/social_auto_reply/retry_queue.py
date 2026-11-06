"""Retry queue helpers for social auto-reply."""
from __future__ import annotations

from app.database import execute, fetch_all


async def due_retries(limit: int = 25) -> list[dict]:
    return await fetch_all(
        """
        SELECT rq.*, q.platform, q.target_id, q.intent
        FROM social_retry_queue rq
        JOIN social_reply_queue q ON q.id = rq.queue_id
        WHERE rq.status='pending' AND rq.next_retry_at <= NOW()
        ORDER BY rq.next_retry_at
        LIMIT $1
        """,
        limit,
    )


async def mark_retry_done(queue_id: int) -> None:
    await execute("UPDATE social_retry_queue SET status='done', updated_at=NOW() WHERE queue_id=$1", queue_id)
