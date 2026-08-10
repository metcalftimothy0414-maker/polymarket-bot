"""Client-side monthly credit budget for The Odds API (docs/ODDS_API_AUDIT.md).

bot.odds_api_usage mirrors what the API says our usage is, but that only
updates when we successfully make a call — if should_call() ever refuses
because we're out of budget, we'd stop calling entirely and never observe
the API's own monthly reset, permanently bricking the feed. This tracks a
local counter keyed by calendar period (YYYY-MM) instead, so it resets
itself at the start of a new month regardless of whether polling resumed.
"""
from __future__ import annotations

import datetime as dt
import sqlite3


def _period(now: str | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc).isoformat())[:7]  # YYYY-MM


def record_call_cost(conn: sqlite3.Connection, cost: int, now: str | None = None) -> None:
    period = _period(now)
    conn.execute(
        "INSERT INTO odds_api_monthly_budget (period, credits_used) VALUES (?, ?) "
        "ON CONFLICT(period) DO UPDATE SET credits_used = credits_used + excluded.credits_used",
        (period, cost),
    )
    conn.commit()


def credits_used_this_period(conn: sqlite3.Connection, now: str | None = None) -> int:
    row = conn.execute(
        "SELECT credits_used FROM odds_api_monthly_budget WHERE period = ?", (_period(now),)
    ).fetchone()
    return row[0] if row else 0


def within_budget(conn: sqlite3.Connection, monthly_credit_budget: int, cost: int = 1, now: str | None = None) -> bool:
    return credits_used_this_period(conn, now) + cost <= monthly_credit_budget
