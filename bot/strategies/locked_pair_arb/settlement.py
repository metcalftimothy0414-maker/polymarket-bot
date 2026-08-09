"""Settlement of locked-pair positions and the predicted-vs-realized
divergence calibration record (build prompt §5.3, §7.2 Stage 4)."""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3

import httpx

from bot.feeds.kalshi import KalshiFeedClient, KalshiResponseError
from bot.feeds.polymarket import PolymarketRestClient
from bot.paper import extract_resolution_outcome

logger = logging.getLogger(__name__)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def extract_kalshi_outcome(market: dict) -> float | None:
    """1.0 if YES won, 0.0 if NO won, None if not yet finalized (or void)."""
    if market.get("status") != "finalized":
        return None
    result = market.get("result")
    if result == "yes":
        return 1.0
    if result == "no":
        return 0.0
    return None  # void/other — leave the position open rather than guess


def settle_pair_position(conn: sqlite3.Connection, pos: sqlite3.Row, k_outcome: float, pm_outcome: float) -> None:
    size = pos["size"]
    if pos["direction"] == "buy_kalshi_yes_poly_no":
        gross_payout = size * (k_outcome + (1 - pm_outcome))
    else:
        gross_payout = size * (pm_outcome + (1 - k_outcome))
    realized_pnl = gross_payout - pos["entry_cost_usd"]
    diverged = k_outcome != pm_outcome

    predicted_edge = pos["predicted_edge_per_contract"]
    edge_error = None
    if predicted_edge is not None and size:
        edge_error = (realized_pnl / size) - predicted_edge

    now = _now()
    conn.execute(
        "INSERT INTO settlements (pair_position_id, pair_id, kalshi_outcome, polymarket_outcome, diverged, "
        "gross_payout_usd, realized_pnl_usd, predicted_edge_per_contract, edge_error_usd, settled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pos["id"], pos["pair_id"], k_outcome, pm_outcome, int(diverged), gross_payout, realized_pnl,
         predicted_edge, edge_error, now),
    )
    conn.execute("UPDATE pair_positions SET status = 'closed', closed_at = ? WHERE id = ?", (now, pos["id"]))
    conn.commit()


async def resolve_pair_positions(
    conn: sqlite3.Connection,
    kalshi_client: KalshiFeedClient,
    rest_client: PolymarketRestClient,
    categories: list[str],
) -> int:
    """Poll both venues for resolution on every open pair_position and settle
    the ones where both sides have finalized."""
    conn.row_factory = sqlite3.Row
    open_positions = conn.execute(
        "SELECT pp.*, p.kalshi_ticker, p.polymarket_slug FROM pair_positions pp "
        "JOIN pairs p ON pp.pair_id = p.id WHERE pp.status = 'open'"
    ).fetchall()
    if not open_positions:
        return 0

    closed_pm_by_slug = {m["slug"]: m for m in await rest_client.discover_markets(categories, closed=True)}
    resolved = 0
    for pos in open_positions:
        pm_market = closed_pm_by_slug.get(pos["polymarket_slug"])
        pm_outcome = extract_resolution_outcome(pm_market) if pm_market else None

        try:
            k_market = await kalshi_client.get_market(pos["kalshi_ticker"])
            k_outcome = extract_kalshi_outcome(k_market)
        except (httpx.HTTPError, KalshiResponseError):
            logger.warning("Kalshi settlement check failed for %s", pos["kalshi_ticker"], exc_info=True)
            k_outcome = None

        if pm_outcome is None or k_outcome is None:
            continue

        settle_pair_position(conn, pos, k_outcome, pm_outcome)
        resolved += 1
    return resolved
