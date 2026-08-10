from __future__ import annotations

import asyncio
import sqlite3
import unittest

from bot.db import SCHEMA
from bot.paper import (
    check_reversal_exits,
    close_position_by_signal,
    close_positions_for_closed_opportunities,
    count_open_positions,
    daily_realized_pnl,
    extract_resolution_outcome,
    has_open_trade_for_opportunity,
    open_position,
    record_unfilled,
    resolve_closed_markets,
    resolve_position,
)
from bot.state import MarketState
from bot.strategies.base import Opportunity


class FakePolymarketWS:
    def __init__(self) -> None:
        self.books: dict[str, dict] = {}

    def is_stale(self, slug: str) -> bool:
        return False


def pm_book(bid, ask):
    return {
        "bids": [{"px": {"value": str(bid)}, "qty": "500"}],
        "offers": [{"px": {"value": str(ask)}, "qty": "500"}],
    }


def make_opportunity(direction: str, entry_price: float, market_ref: str = "pm-slug") -> Opportunity:
    return Opportunity(
        strategy_id="divergence", params_hash="abc123", detected_at="now",
        market_ref=market_ref, direction=direction, signal_value=0.05,
        entry_price=entry_price, top_levels_json="{}",
    )


class TestFillSimulator(unittest.TestCase):
    def setUp(self):
        self.ws = FakePolymarketWS()
        self.state = MarketState(self.ws)

    def test_fills_when_book_trades_through_limit(self):
        from bot.paper import FillSimulator
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        opp = make_opportunity("buy_polymarket", 0.51)
        filled, price = asyncio.run(FillSimulator(self.state).try_fill(opp, timeout_seconds=0.05, poll_interval=0.01))
        self.assertTrue(filled)
        self.assertEqual(price, 0.51)

    def test_times_out_when_price_never_available(self):
        from bot.paper import FillSimulator
        self.ws.books["pm-slug"] = pm_book(0.60, 0.61)  # ask never comes down to 0.51
        opp = make_opportunity("buy_polymarket", 0.51)
        filled, price = asyncio.run(FillSimulator(self.state).try_fill(opp, timeout_seconds=0.03, poll_interval=0.01))
        self.assertFalse(filled)
        self.assertIsNone(price)

    def test_sell_direction_fills_on_bid(self):
        from bot.paper import FillSimulator
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        opp = make_opportunity("sell_polymarket", 0.50)
        filled, price = asyncio.run(FillSimulator(self.state).try_fill(opp, timeout_seconds=0.05, poll_interval=0.01))
        self.assertTrue(filled)
        self.assertEqual(price, 0.50)

    def test_no_book_never_fills(self):
        from bot.paper import FillSimulator
        opp = make_opportunity("buy_polymarket", 0.51, market_ref="unknown-slug")
        filled, price = asyncio.run(FillSimulator(self.state).try_fill(opp, timeout_seconds=0.03, poll_interval=0.01))
        self.assertFalse(filled)


class TestPositionLifecycle(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.conn.row_factory = sqlite3.Row

    def test_open_position_computes_entry_fee(self):
        opp = make_opportunity("buy_polymarket", 0.50)
        trade_id = open_position(self.conn, opp, fill_price=0.50, notional_usd=10)
        trade = self.conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        shares = 10 / 0.50
        self.assertAlmostEqual(trade["entry_fee"], shares * 0.06 * 0.50 * 0.50)
        self.assertEqual(trade["status"], "open")

    def test_sportsbook_divergence_opportunity_cannot_be_promoted(self):
        # sportsbook_divergence is a reference signal (bot.strategies.base.
        # NON_TRADEABLE_STRATEGY_IDS) — open_position must refuse it
        # regardless of config, since this is the actual promotion point.
        opp = Opportunity(
            strategy_id="sportsbook_divergence", params_hash="abc123", detected_at="now",
            market_ref="pm-slug", direction="buy_polymarket", signal_value=0.05,
            entry_price=0.50, top_levels_json="{}",
        )
        with self.assertRaises(ValueError):
            open_position(self.conn, opp, fill_price=0.50, notional_usd=10)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 0)

    def test_close_by_signal_profitable_long(self):
        opp = make_opportunity("buy_polymarket", 0.50)
        trade_id = open_position(self.conn, opp, fill_price=0.50, notional_usd=10)
        trade = self.conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        close_position_by_signal(self.conn, trade, exit_price=0.60)
        row = self.conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        self.assertEqual(row["status"], "closed")
        self.assertGreater(row["realized_pnl_usd"], 0)  # bought at 0.50, exited at 0.60 -> profit

    def test_close_by_signal_losing_short(self):
        opp = make_opportunity("sell_polymarket", 0.50)
        trade_id = open_position(self.conn, opp, fill_price=0.50, notional_usd=10)
        trade = self.conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        close_position_by_signal(self.conn, trade, exit_price=0.60)  # price rose against the short
        row = self.conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        self.assertLess(row["realized_pnl_usd"], 0)

    def test_resolve_position_win(self):
        opp = make_opportunity("buy_polymarket", 0.50)
        trade_id = open_position(self.conn, opp, fill_price=0.50, notional_usd=10)
        trade = self.conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        resolve_position(self.conn, trade, outcome=1.0)
        row = self.conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        self.assertEqual(row["exit_reason"], "resolution")
        self.assertEqual(row["exit_fee"], 0)
        self.assertGreater(row["realized_pnl_usd"], 0)

    def test_resolve_position_loss(self):
        opp = make_opportunity("buy_polymarket", 0.50)
        trade_id = open_position(self.conn, opp, fill_price=0.50, notional_usd=10)
        trade = self.conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        resolve_position(self.conn, trade, outcome=0.0)
        row = self.conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        self.assertLess(row["realized_pnl_usd"], 0)

    def test_record_unfilled(self):
        opp = make_opportunity("buy_polymarket", 0.50)
        record_unfilled(self.conn, opp, notional_usd=10)
        row = self.conn.execute("SELECT * FROM paper_trades").fetchone()
        self.assertEqual(row["status"], "unfilled")
        self.assertIsNone(row["fill_price"])


class TestResolutionExtraction(unittest.TestCase):
    def test_extracts_long_side_price_when_closed(self):
        market = {"closed": True, "marketSides": [{"long": True, "price": "1"}, {"long": False, "price": "0"}]}
        self.assertEqual(extract_resolution_outcome(market), 1.0)

    def test_none_when_not_closed(self):
        market = {"closed": False, "marketSides": [{"long": True, "price": "1"}]}
        self.assertIsNone(extract_resolution_outcome(market))

    def test_none_when_no_long_side(self):
        market = {"closed": True, "marketSides": [{"long": False, "price": "0"}]}
        self.assertIsNone(extract_resolution_outcome(market))

    def test_none_when_price_key_missing_entirely(self):
        # Found against real live data: some genuinely-closed Polymarket markets
        # have a long side with no "price" key at all, not just an empty one.
        market = {"closed": True, "marketSides": [{"long": True}]}
        self.assertIsNone(extract_resolution_outcome(market))


class FakeRestClient:
    def __init__(self, closed_markets):
        self._closed_markets = closed_markets

    async def discover_markets(self, categories, closed):
        return self._closed_markets


class TestResolveClosedMarkets(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_resolves_matching_open_trades(self):
        opp = make_opportunity("buy_polymarket", 0.50, market_ref="pm-slug")
        open_position(self.conn, opp, fill_price=0.50, notional_usd=10)

        closed_markets = [{
            "slug": "pm-slug", "closed": True,
            "marketSides": [{"long": True, "price": "1"}, {"long": False, "price": "0"}],
        }]
        rest = FakeRestClient(closed_markets)
        resolved_count = asyncio.run(resolve_closed_markets(self.conn, rest, ["sports"]))
        self.assertEqual(resolved_count, 1)
        row = self.conn.execute("SELECT status FROM paper_trades").fetchone()
        self.assertEqual(row[0], "closed")

    def test_ignores_unrelated_closed_markets(self):
        opp = make_opportunity("buy_polymarket", 0.50, market_ref="pm-slug")
        open_position(self.conn, opp, fill_price=0.50, notional_usd=10)

        closed_markets = [{
            "slug": "some-other-slug", "closed": True,
            "marketSides": [{"long": True, "price": "1"}],
        }]
        rest = FakeRestClient(closed_markets)
        resolved_count = asyncio.run(resolve_closed_markets(self.conn, rest, ["sports"]))
        self.assertEqual(resolved_count, 0)
        row = self.conn.execute("SELECT status FROM paper_trades").fetchone()
        self.assertEqual(row[0], "open")


class TestCloseForClosedOpportunities(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.ws = FakePolymarketWS()
        self.state = MarketState(self.ws)

    def _seed_opportunity_and_trade(self, direction, fill_price, opp_status="open"):
        now = "2026-01-01T00:00:00+00:00"
        cur = self.conn.execute(
            "INSERT INTO opportunities (strategy_id, params_hash, market_ref, direction, signal_value, "
            "entry_price, detected_at, last_seen_at, status) VALUES ('divergence','h','pm-slug',?,0.05,?,?,?,?)",
            (direction, fill_price, now, now, opp_status),
        )
        opp = make_opportunity(direction, fill_price)
        opp.opportunity_id = cur.lastrowid
        trade_id = open_position(self.conn, opp, fill_price=fill_price, notional_usd=10)
        self.conn.commit()
        return trade_id

    def test_closes_long_trade_when_opportunity_closes(self):
        trade_id = self._seed_opportunity_and_trade("buy_polymarket", 0.50, opp_status="closed")
        self.ws.books["pm-slug"] = pm_book(0.55, 0.56)  # exit a long at the bid
        closed = close_positions_for_closed_opportunities(self.conn, self.state)
        self.assertEqual(closed, 1)
        row = self.conn.execute("SELECT status, exit_price FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        self.assertEqual(row[0], "closed")
        self.assertEqual(row[1], 0.55)

    def test_leaves_trade_open_while_opportunity_still_open(self):
        self._seed_opportunity_and_trade("buy_polymarket", 0.50, opp_status="open")
        self.ws.books["pm-slug"] = pm_book(0.55, 0.56)
        closed = close_positions_for_closed_opportunities(self.conn, self.state)
        self.assertEqual(closed, 0)


class TestReversalExits(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.ws = FakePolymarketWS()
        self.state = MarketState(self.ws)

    def test_closes_long_on_adverse_reversal_past_threshold(self):
        opp = make_opportunity("buy_polymarket", 0.50)
        trade_id = open_position(self.conn, opp, fill_price=0.50, notional_usd=10)
        self.conn.execute("UPDATE paper_trades SET strategy_id='sports_momentum' WHERE id=?", (trade_id,))
        self.conn.commit()
        self.ws.books["pm-slug"] = pm_book(0.45, 0.46)  # dropped 5c, past a 4c reversal threshold
        closed = check_reversal_exits(self.conn, self.state, exit_reversal_cents=4, strategy_id="sports_momentum")
        self.assertEqual(closed, 1)
        row = self.conn.execute("SELECT exit_reason FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        self.assertEqual(row[0], "reversal_exit")

    def test_does_not_close_within_reversal_tolerance(self):
        opp = make_opportunity("buy_polymarket", 0.50)
        trade_id = open_position(self.conn, opp, fill_price=0.50, notional_usd=10)
        self.conn.execute("UPDATE paper_trades SET strategy_id='sports_momentum' WHERE id=?", (trade_id,))
        self.conn.commit()
        self.ws.books["pm-slug"] = pm_book(0.48, 0.49)  # only dropped 2c, under the 4c threshold
        closed = check_reversal_exits(self.conn, self.state, exit_reversal_cents=4, strategy_id="sports_momentum")
        self.assertEqual(closed, 0)


class TestRiskHelpers(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_count_open_positions(self):
        open_position(self.conn, make_opportunity("buy_polymarket", 0.50), 0.50, 10)
        open_position(self.conn, make_opportunity("buy_polymarket", 0.50, "other-slug"), 0.50, 10)
        self.assertEqual(count_open_positions(self.conn), 2)

    def test_has_open_trade_for_opportunity(self):
        opp = make_opportunity("buy_polymarket", 0.50)
        opp.opportunity_id = 42
        open_position(self.conn, opp, 0.50, 10)
        self.assertTrue(has_open_trade_for_opportunity(self.conn, 42))
        self.assertFalse(has_open_trade_for_opportunity(self.conn, 999))
        self.assertFalse(has_open_trade_for_opportunity(self.conn, None))

    def test_daily_realized_pnl_sums_closed_trades_only(self):
        self.conn.row_factory = sqlite3.Row
        opp = make_opportunity("buy_polymarket", 0.50)
        trade_id = open_position(self.conn, opp, 0.50, 10)
        trade = self.conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        close_position_by_signal(self.conn, trade, exit_price=0.60)  # profitable close

        open_position(self.conn, make_opportunity("buy_polymarket", 0.50, "still-open-slug"), 0.50, 10)

        pnl = daily_realized_pnl(self.conn, "divergence")
        self.assertGreater(pnl, 0)  # only the closed trade counts, open one is ignored


if __name__ == "__main__":
    unittest.main()
