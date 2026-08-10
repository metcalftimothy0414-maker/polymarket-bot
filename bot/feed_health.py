"""Feed-level health tracking: DEGRADED vs IDLE are not the same thing.

IDLE means the feed is working and just has nothing new to report — the
previous, and still correct, meaning of a quiet strategy panel. DEGRADED
means the feed itself is failing (auth error, quota exhausted, persistent
network errors) and must never look the same as IDLE on the dashboard —
that's exactly how sportsbook_divergence's exhausted Odds API quota went
unnoticed (docs/ODDS_API_AUDIT.md).
"""
from __future__ import annotations

import datetime as dt
import sqlite3

FEED_NAMES = ("polymarket", "kalshi", "odds_api")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def record_success(conn: sqlite3.Connection, feed_name: str, now: str | None = None) -> None:
    now = now or _now()
    conn.execute(
        "INSERT INTO feed_health (feed_name, last_success_at, last_error_at, last_error_message, updated_at) "
        "VALUES (?, ?, NULL, NULL, ?) ON CONFLICT(feed_name) DO UPDATE SET "
        "last_success_at = excluded.last_success_at, updated_at = excluded.updated_at",
        (feed_name, now, now),
    )
    conn.commit()


def record_error(conn: sqlite3.Connection, feed_name: str, message: str, now: str | None = None) -> None:
    now = now or _now()
    conn.execute(
        "INSERT INTO feed_health (feed_name, last_success_at, last_error_at, last_error_message, updated_at) "
        "VALUES (?, NULL, ?, ?, ?) ON CONFLICT(feed_name) DO UPDATE SET "
        "last_error_at = excluded.last_error_at, last_error_message = excluded.last_error_message, "
        "updated_at = excluded.updated_at",
        (feed_name, now, message[:500], now),
    )
    conn.commit()


def feed_status(conn: sqlite3.Connection, feed_name: str) -> dict:
    """DEGRADED if the most recent event was an error (or there's never
    been a success at all); otherwise IDLE — the feed may or may not have
    fresh data, but it isn't observably broken."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_success_at, last_error_at, last_error_message FROM feed_health WHERE feed_name = ?",
        (feed_name,),
    ).fetchone()
    if row is None:
        return {"status": "IDLE", "last_success_at": None, "last_error_at": None, "last_error_message": None}

    last_success = row["last_success_at"]
    last_error = row["last_error_at"]
    is_degraded = last_error is not None and (last_success is None or last_error > last_success)
    return {
        "status": "DEGRADED" if is_degraded else "IDLE",
        "last_success_at": last_success,
        "last_error_at": last_error,
        "last_error_message": row["last_error_message"] if is_degraded else None,
    }


def all_feed_statuses(conn: sqlite3.Connection) -> dict[str, dict]:
    return {name: feed_status(conn, name) for name in FEED_NAMES}
