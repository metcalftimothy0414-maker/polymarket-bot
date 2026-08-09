"""Full-catalog discovery for both venues (§2 of the category expansion
task). Metadata only — never touches the book-polling path, and runs on
its own hourly cadence separate from the 5s scan loop.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3

from bot.feeds.kalshi import KalshiFeedClient
from bot.feeds.polymarket import PolymarketRestClient

logger = logging.getLogger(__name__)

CATALOG_REFRESH_INTERVAL_SECONDS = 3600


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def kalshi_series_ticker(market_ticker_or_event: str) -> str:
    """Kalshi series tickers never contain a hyphen (confirmed against
    every ticker seen this session — KXMLBGAME, KXNBAGAME, KXBTCY,
    SENATEIA, ...); both market tickers (SERIES-EVENT-SIDE) and event
    tickers (SERIES-EVENT) share that same prefix up to the first hyphen."""
    return market_ticker_or_event.split("-", 1)[0]


async def discover_kalshi_catalog(kalshi_client: KalshiFeedClient, conn: sqlite3.Connection) -> int:
    """One flat paginated scan across every open Kalshi market (not a
    per-series loop — ~12.6k series would mean ~12.6k requests; this is a
    few dozen at limit=1000). category comes from a join against
    kalshi_series_multipliers' source data... but that table only stores
    fee fields, not category, so category is looked up separately via a
    lightweight in-memory series->category map built from one
    get_series_list() call, keyed the same way the fee multiplier table is."""
    now = _now()
    series_category: dict[str, str] = {}
    for s in await kalshi_client.get_series_list():
        ticker = s.get("ticker")
        category = s.get("category")
        if ticker and category:
            series_category[ticker] = category

    markets = await kalshi_client.get_markets_bulk(status="open")
    written = 0
    for m in markets:
        ticker = m.get("ticker")
        if not ticker:
            continue
        event_ticker = m.get("event_ticker", "")
        series_ticker = kalshi_series_ticker(event_ticker or ticker)
        category = series_category.get(series_ticker)
        conn.execute(
            "INSERT OR REPLACE INTO kalshi_catalog (ticker, series_ticker, event_ticker, title, category, "
            "rules_primary, rules_secondary, close_time, expiration_time, occurrence_datetime, status, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "COALESCE((SELECT first_seen FROM kalshi_catalog WHERE ticker = ?), ?), ?)",
            (
                ticker, series_ticker, event_ticker, m.get("title"), category,
                m.get("rules_primary"), m.get("rules_secondary"), m.get("close_time"),
                m.get("expiration_time"), m.get("occurrence_datetime"), m.get("status"),
                ticker, now, now,
            ),
        )
        written += 1
    conn.commit()
    logger.info("Discovered %d open Kalshi markets across %d series", written, len(series_category))
    return written


async def discover_polymarket_catalog(rest_client: PolymarketRestClient, conn: sqlite3.Connection) -> int:
    """Full-catalog scan across every category via PolymarketRestClient.discover_all_markets()."""
    now = _now()
    markets = await rest_client.discover_all_markets(closed=False)
    written = 0
    for m in markets:
        slug = m.get("slug")
        if not slug:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO polymarket_catalog (slug, question, category, market_type, description, "
            "tick_size, neg_risk, game_start_time, end_date, status, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "COALESCE((SELECT first_seen FROM polymarket_catalog WHERE slug = ?), ?), ?)",
            (
                slug, m.get("question"), m.get("category"), m.get("marketType"), m.get("description"),
                m.get("orderPriceMinTickSize"), int(bool(m.get("negRisk"))), m.get("gameStartTime"),
                m.get("endDate"), "closed" if m.get("closed") else "open",
                slug, now, now,
            ),
        )
        written += 1
    conn.commit()
    logger.info("Discovered %d open Polymarket US markets", written)
    return written


async def catalog_refresh_loop(
    kalshi_client: KalshiFeedClient | None,
    rest_client: PolymarketRestClient,
    conn: sqlite3.Connection,
    stop_event: asyncio.Event | None = None,
    interval_seconds: float = CATALOG_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Runs both discovery scans back to back, then sleeps. Separate task
    from the 5s scan loop and from the per-ticker orderbook poll — this is
    metadata enumeration, not book polling, and must not compete with it."""
    while stop_event is None or not stop_event.is_set():
        try:
            if kalshi_client is not None:
                await discover_kalshi_catalog(kalshi_client, conn)
            await discover_polymarket_catalog(rest_client, conn)
        except Exception:
            logger.exception("Catalog discovery failed")
        await asyncio.sleep(interval_seconds)
