from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import time

from bot.fees import taker_fee
from bot.feeds.polymarket import PolymarketRestClient
from bot.pricing import polymarket_best_bid_ask
from bot.state import MarketState
from bot.strategies.base import Opportunity


class FillSimulator:
    """Watches the live book and fills only if it actually trades through the
    limit price on a LATER snapshot than the one that triggered the signal —
    no optimistic fill on the same tick the opportunity was detected, since
    by the time a resting order is live the book may have already moved.
    """

    def __init__(self, state: MarketState) -> None:
        self.state = state

    async def try_fill(
        self, opportunity: Opportunity, timeout_seconds: float, poll_interval: float = 1.0,
    ) -> tuple[bool, float | None]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(min(poll_interval, max(deadline - time.monotonic(), 0)))
            book = self.state.polymarket_book(opportunity.market_ref)
            if not book:
                continue
            bid, ask = polymarket_best_bid_ask(book)
            if opportunity.direction == "buy_polymarket" and ask is not None and ask <= opportunity.entry_price:
                return True, ask
            if opportunity.direction == "sell_polymarket" and bid is not None and bid >= opportunity.entry_price:
                return True, bid
        return False, None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def count_open_positions(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status IN ('open', 'pending_fill')").fetchone()[0]


def has_open_trade_for_opportunity(conn: sqlite3.Connection, opportunity_id: int | None) -> bool:
    if opportunity_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM paper_trades WHERE opportunity_id = ? AND status IN ('open', 'pending_fill')",
        (opportunity_id,),
    ).fetchone()
    return row is not None


def daily_realized_pnl(conn: sqlite3.Connection, strategy_id: str) -> float:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_usd), 0) FROM paper_trades "
        "WHERE strategy_id = ? AND status = 'closed' AND closed_at >= ?",
        (strategy_id, today),
    ).fetchone()
    return row[0]


def open_position(
    conn: sqlite3.Connection, opportunity: Opportunity, fill_price: float, notional_usd: float,
) -> int:
    shares = notional_usd / fill_price
    entry_fee = shares * taker_fee(fill_price)
    now = _now()
    cur = conn.execute(
        "INSERT INTO paper_trades "
        "(strategy_id, params_hash, opportunity_id, market_ref, direction, signal_entry_price, fill_price, "
        "notional_usd, entry_fee, status, opened_at, filled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
        (opportunity.strategy_id, opportunity.params_hash, opportunity.opportunity_id, opportunity.market_ref,
         opportunity.direction, opportunity.entry_price, fill_price, notional_usd, entry_fee, now, now),
    )
    conn.commit()
    return cur.lastrowid


def record_unfilled(conn: sqlite3.Connection, opportunity: Opportunity, notional_usd: float) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO paper_trades "
        "(strategy_id, params_hash, opportunity_id, market_ref, direction, signal_entry_price, notional_usd, "
        "status, opened_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'unfilled', ?)",
        (opportunity.strategy_id, opportunity.params_hash, opportunity.opportunity_id, opportunity.market_ref,
         opportunity.direction, opportunity.entry_price, notional_usd, now),
    )
    conn.commit()


def _pnl_per_share(direction: str, fill_price: float, exit_price: float) -> float:
    # ponytail: simplified short accounting — sell_polymarket P&L uses the same
    # shares-per-notional convention as the long side rather than modeling
    # separate NO-token economics. Fine for a falsification test's edge
    # direction/magnitude; revisit if this graduates past Phase 1.
    return (exit_price - fill_price) if direction == "buy_polymarket" else (fill_price - exit_price)


def close_position_by_signal(conn: sqlite3.Connection, trade: sqlite3.Row, exit_price: float) -> None:
    shares = trade["notional_usd"] / trade["fill_price"]
    exit_fee = shares * taker_fee(exit_price)
    pnl = shares * _pnl_per_share(trade["direction"], trade["fill_price"], exit_price) - trade["entry_fee"] - exit_fee
    conn.execute(
        "UPDATE paper_trades SET status='closed', exit_price=?, exit_fee=?, exit_reason='signal_exit', "
        "realized_pnl_usd=?, closed_at=? WHERE id=?",
        (exit_price, exit_fee, pnl, _now(), trade["id"]),
    )
    conn.commit()


def close_position_by_reversal(conn: sqlite3.Connection, trade: sqlite3.Row, exit_price: float) -> None:
    shares = trade["notional_usd"] / trade["fill_price"]
    exit_fee = shares * taker_fee(exit_price)
    pnl = shares * _pnl_per_share(trade["direction"], trade["fill_price"], exit_price) - trade["entry_fee"] - exit_fee
    conn.execute(
        "UPDATE paper_trades SET status='closed', exit_price=?, exit_fee=?, exit_reason='reversal_exit', "
        "realized_pnl_usd=?, closed_at=? WHERE id=?",
        (exit_price, exit_fee, pnl, _now(), trade["id"]),
    )
    conn.commit()


def close_positions_for_closed_opportunities(conn: sqlite3.Connection, state: MarketState) -> int:
    """An opportunity closing (divergence/momentum condition no longer holds) is
    the exit trigger for any open paper trade correlated to it."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT pt.* FROM paper_trades pt JOIN opportunities o ON pt.opportunity_id = o.id "
        "WHERE pt.status = 'open' AND o.status = 'closed'"
    ).fetchall()
    closed = 0
    for trade in rows:
        book = state.polymarket_book(trade["market_ref"])
        if not book:
            continue
        bid, ask = polymarket_best_bid_ask(book)
        # Exiting is the opposite side of entering: sell a long at the bid, buy back a short at the ask.
        exit_price = bid if trade["direction"] == "buy_polymarket" else ask
        if exit_price is None:
            continue
        close_position_by_signal(conn, trade, exit_price)
        closed += 1
    return closed


def check_reversal_exits(
    conn: sqlite3.Connection, state: MarketState, exit_reversal_cents: float, strategy_id: str,
) -> int:
    """Strategy C's early-exit rule: bail if price reverses through entry by
    more than exit_reversal_cents, rather than holding to resolution."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id = ?", (strategy_id,)
    ).fetchall()
    closed = 0
    for trade in rows:
        book = state.polymarket_book(trade["market_ref"])
        if not book:
            continue
        bid, ask = polymarket_best_bid_ask(book)
        if trade["direction"] == "buy_polymarket":
            current, reversal_cents = bid, (trade["fill_price"] - bid) * 100 if bid is not None else None
        else:
            current, reversal_cents = ask, (ask - trade["fill_price"]) * 100 if ask is not None else None
        if current is None or reversal_cents is None:
            continue
        if reversal_cents >= exit_reversal_cents:
            close_position_by_reversal(conn, trade, current)
            closed += 1
    return closed


def resolve_position(conn: sqlite3.Connection, trade: sqlite3.Row, outcome: float) -> None:
    """outcome: 1.0 if the long side won, 0.0 if it lost. No exit fee — settlement
    to final payout isn't itself a taker trade."""
    shares = trade["notional_usd"] / trade["fill_price"]
    pnl = shares * _pnl_per_share(trade["direction"], trade["fill_price"], outcome) - trade["entry_fee"]
    conn.execute(
        "UPDATE paper_trades SET status='closed', exit_price=?, exit_fee=0, exit_reason='resolution', "
        "realized_pnl_usd=?, closed_at=? WHERE id=?",
        (outcome, pnl, _now(), trade["id"]),
    )
    conn.commit()


def extract_resolution_outcome(market_json: dict) -> float | None:
    """A closed Polymarket market's long-side marketSides[].price IS the final
    settlement (1.0 won, 0.0 lost) — no separate resolution feed needed."""
    if not market_json.get("closed"):
        return None
    for side in market_json.get("marketSides", []):
        if side.get("long"):
            try:
                return float(side["price"])
            except (TypeError, ValueError):
                return None
    return None


async def resolve_closed_markets(
    conn: sqlite3.Connection, rest_client: PolymarketRestClient, categories: list[str],
) -> int:
    """Poll for newly-closed markets and settle any open paper_trades against them."""
    closed_markets = await rest_client.discover_markets(categories, closed=True)
    conn.row_factory = sqlite3.Row
    resolved = 0
    for market in closed_markets:
        outcome = extract_resolution_outcome(market)
        if outcome is None:
            continue
        open_trades = conn.execute(
            "SELECT * FROM paper_trades WHERE market_ref = ? AND status = 'open'", (market["slug"],)
        ).fetchall()
        for trade in open_trades:
            resolve_position(conn, trade, outcome)
            resolved += 1
    return resolved
