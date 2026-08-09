"""Populates the Kalshi per-series fee multiplier table (§1 of the category
expansion task) from Kalshi's live catalog, persists it to SQLite, and loads
it into bot.edge's in-memory lookup so kalshi_fee() never has to guess.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from decimal import Decimal

from bot.feeds.kalshi import KalshiFeedClient

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 24 * 3600

# Confirmed 0/0 (zero-fee) series per Kalshi's Non-Standard Fees table.
ZERO_FEE_SERIES: set[str] = {
    "KXBTCY", "KXETHY", "KXGREENLAND", "KXDOED", "KXLAYOFFSYINFO",
    "KXCITRINI", "KXELECTIRAN", "KXIRANDEMOCRACY", "KXPAHLAVIHEAD", "KXGAMBLINGREPEAL",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def refresh_kalshi_multipliers(kalshi_client: KalshiFeedClient, conn: sqlite3.Connection) -> int:
    """Fetches Kalshi's Sports-category series (M_taker=1, M_maker=1) and
    combines it with the known 0/0 list, upserting both into
    kalshi_series_multipliers. Series not covered by either keep the raw
    published default (1/0) — not written here; bot.edge applies that
    default itself and logs the first time an unconfirmed series is used."""
    now = _now()
    rows: list[tuple[str, Decimal, Decimal, str]] = []

    sports_series = await kalshi_client.get_series_list(category="Sports")
    for s in sports_series:
        ticker = s.get("ticker")
        if ticker:
            rows.append((ticker, Decimal(1), Decimal(1), "category_sports"))

    for ticker in ZERO_FEE_SERIES:
        rows.append((ticker, Decimal(0), Decimal(0), "known_zero_fee_list"))

    for ticker, m_taker, m_maker, source in rows:
        conn.execute(
            "INSERT INTO kalshi_series_multipliers (series_ticker, m_taker, m_maker, source, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(series_ticker) DO UPDATE SET "
            "m_taker=excluded.m_taker, m_maker=excluded.m_maker, source=excluded.source, fetched_at=excluded.fetched_at",
            (ticker, float(m_taker), float(m_maker), source, now),
        )
    conn.commit()
    logger.info("Refreshed %d Kalshi series fee multipliers (%d sports, %d zero-fee)", len(rows), len(sports_series), len(ZERO_FEE_SERIES))
    return len(rows)


def load_into_edge_module(conn: sqlite3.Connection) -> int:
    """Reads kalshi_series_multipliers and populates bot.edge's in-memory
    lookup so kalshi_fee() reflects the latest refresh without every call
    site needing DB access. Mutates the dict in place (not a reassignment)
    so anything that imported KALSHI_SERIES_MULTIPLIERS by reference still
    sees the update."""
    from bot import edge

    rows = conn.execute("SELECT series_ticker, m_taker, m_maker FROM kalshi_series_multipliers").fetchall()
    edge.KALSHI_SERIES_MULTIPLIERS.clear()
    for ticker, m_taker, m_maker in rows:
        edge.KALSHI_SERIES_MULTIPLIERS[ticker] = (Decimal(str(m_taker)), Decimal(str(m_maker)))
    return len(rows)


def _seconds_since_last_refresh(conn: sqlite3.Connection) -> float | None:
    row = conn.execute("SELECT MAX(fetched_at) FROM kalshi_series_multipliers").fetchone()
    if not row or not row[0]:
        return None
    fetched = dt.datetime.fromisoformat(row[0])
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - fetched).total_seconds()


async def daily_refresh_loop(
    kalshi_client: KalshiFeedClient, conn: sqlite3.Connection, stop_event: asyncio.Event | None = None,
    check_interval_seconds: float = 3600,
) -> None:
    """Refreshes at startup (if stale) and then checks hourly whether
    REFRESH_INTERVAL_SECONDS has elapsed since the last refresh."""
    while stop_event is None or not stop_event.is_set():
        age = _seconds_since_last_refresh(conn)
        if age is None or age >= REFRESH_INTERVAL_SECONDS:
            try:
                await refresh_kalshi_multipliers(kalshi_client, conn)
                load_into_edge_module(conn)
            except Exception:
                logger.exception("Kalshi fee multiplier refresh failed")
        await asyncio.sleep(check_interval_seconds)
