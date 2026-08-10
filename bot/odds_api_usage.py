"""The Odds API credit usage tracking (docs/ODDS_API_AUDIT.md).

x-requests-remaining/x-requests-used are present on every response, success
or error — recording them here is what makes odds_api_credits_remaining a
real dashboard field instead of a number nobody can see until the quota
silently runs out.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import httpx


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def record_usage(conn: sqlite3.Connection, headers: httpx.Headers, endpoint: str, now: str | None = None) -> None:
    remaining = headers.get("x-requests-remaining")
    used = headers.get("x-requests-used")
    if remaining is None and used is None:
        return  # not an Odds API response (or a malformed one) — nothing to record
    conn.execute(
        "INSERT INTO odds_api_usage (ts, credits_remaining, credits_used, endpoint) VALUES (?, ?, ?, ?)",
        (now or _now(), int(remaining) if remaining is not None else None, int(used) if used is not None else None, endpoint),
    )
    conn.commit()


def latest_usage(conn: sqlite3.Connection) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT ts, credits_remaining, credits_used FROM odds_api_usage ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
