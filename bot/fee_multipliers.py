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

# fee_type suffix that means maker fees apply at all on this series (a plain
# "quadratic" series has M_maker=0 regardless of its fee_multiplier).
MAKER_FEE_TYPE_SUFFIX = "_with_maker_fees"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _multipliers_from_series(s: dict) -> tuple[Decimal, Decimal] | None:
    """Kalshi's /series response carries the multiplier directly — no need
    to infer it from category. Confirmed live 2026-08-09 against every
    series named in the task spec: fee_multiplier IS M_taker; M_maker
    equals it too but only when fee_type ends in "_with_maker_fees"
    (e.g. KXMLBGAME: fee_multiplier=0.5, "quadratic_with_maker_fees" ->
    0.5/0.5 — NOT the flat 1/1 every sports series was assumed to carry;
    KXNFLPASSYDS: fee_multiplier=1, plain "quadratic" -> 1/0 despite being
    Sports category; KXBTCY/KXGREENLAND/etc: fee_multiplier=0 -> 0/0).
    Returns None when the series has no fee data (some inactive/legacy
    tickers return null fields) — those are simply not written, so the
    caller's own default + warn-once behavior applies."""
    raw_multiplier = s.get("fee_multiplier")
    fee_type = s.get("fee_type")
    if raw_multiplier is None or fee_type is None:
        return None
    m_taker = Decimal(str(raw_multiplier))
    m_maker = m_taker if fee_type.endswith(MAKER_FEE_TYPE_SUFFIX) else Decimal(0)
    return m_taker, m_maker


async def refresh_kalshi_multipliers(kalshi_client: KalshiFeedClient, conn: sqlite3.Connection) -> int:
    """Fetches Kalshi's full series catalog (~12.6k series in one call —
    /series does not paginate the way /markets does) and upserts every
    series with usable fee data into kalshi_series_multipliers."""
    now = _now()
    all_series = await kalshi_client.get_series_list()
    written = 0

    for s in all_series:
        ticker = s.get("ticker")
        multipliers = _multipliers_from_series(s)
        if not ticker or multipliers is None:
            continue
        m_taker, m_maker = multipliers
        conn.execute(
            "INSERT INTO kalshi_series_multipliers (series_ticker, m_taker, m_maker, source, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(series_ticker) DO UPDATE SET "
            "m_taker=excluded.m_taker, m_maker=excluded.m_maker, source=excluded.source, fetched_at=excluded.fetched_at",
            (ticker, float(m_taker), float(m_maker), "kalshi_series_api", now),
        )
        written += 1
    conn.commit()
    logger.info("Refreshed %d/%d Kalshi series fee multipliers from live catalog", written, len(all_series))
    return written


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
