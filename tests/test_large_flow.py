from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import unittest

from bot.db import SCHEMA
from bot.state import MarketState
from bot.strategies.large_flow import MIN_TRADE_SAMPLES, LargeFlowStrategy


class FakePolymarketWS:
    def __init__(self) -> None:
        self.books: dict[str, dict] = {}
        self.trades: dict[str, list[dict]] = {}
        self._stale: set[str] = set()

    def is_stale(self, slug: str) -> bool:
        return slug in self._stale


def pm_book(bid, ask, qty=2000):
    return {
        "bids": [{"px": {"value": str(bid)}, "qty": str(qty)}],
        "offers": [{"px": {"value": str(ask)}, "qty": str(qty)}],
    }


def iso(delta_seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta_seconds)).isoformat()


def trade(size_usd: float, action: str, ts: str, outcome_side: str = "OUTCOME_SIDE_YES") -> dict:
    return {
        "marketSlug": "pm-slug",
        "price": {"value": "0.5000", "currency": "USD"},
        "quantity": {"value": str(size_usd), "currency": "USD"},
        "tradeTime": ts,
        "maker": {"side": "ORDER_SIDE_SELL" if action == "ORDER_ACTION_BUY" else "ORDER_SIDE_BUY",
                  "intent": "ORDER_INTENT_SELL_LONG", "outcomeSide": outcome_side, "action": "ORDER_ACTION_SELL"},
        "taker": {"side": "ORDER_SIDE_BUY" if action == "ORDER_ACTION_BUY" else "ORDER_SIDE_SELL",
                  "intent": "ORDER_INTENT_BUY_LONG", "outcomeSide": outcome_side, "action": action},
    }


class TestLargeFlowStrategy(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO markets (slug, question, category, end_date, game_start_time, active, closed, "
            "raw_json, discovered_at, updated_at) VALUES (?, 'q', 'sports', ?, ?, 1, 0, '', 'now', 'now')",
            ("pm-slug", iso(1800), iso(-600)),
        )
        self.conn.commit()

        self.ws = FakePolymarketWS()
        self.state = MarketState(self.ws)
        self.strategy = LargeFlowStrategy(self.conn, self.state, market_slugs=["pm-slug"])

    def _scan(self):
        return asyncio.run(self.strategy.scan())

    def _seed_baseline(self, size: float = 50.0, n: int = MIN_TRADE_SAMPLES):
        self.ws.trades["pm-slug"] = [trade(size, "ORDER_ACTION_BUY", iso(-100 + i)) for i in range(n)]

    def test_no_signal_without_enough_trade_history(self):
        self.ws.trades["pm-slug"] = [trade(50.0, "ORDER_ACTION_BUY", iso(-1))]
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        opps = self._scan()
        self.assertEqual(opps, [])
        candidates = self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        self.assertEqual(candidates, 1)  # still skip-logged, just with no multiple

    def test_opens_opportunity_on_large_aggressive_buy_with_filters_passing(self):
        self._seed_baseline(size=50.0)
        self.ws.trades["pm-slug"].append(trade(600.0, "ORDER_ACTION_BUY", iso(0)))  # 12x median
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        opps = self._scan()
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0].direction, "buy_polymarket")
        self.assertAlmostEqual(opps[0].signal_value, 12.0)

    def test_direction_follows_taker_sell(self):
        self._seed_baseline(size=50.0)
        self.ws.trades["pm-slug"].append(trade(600.0, "ORDER_ACTION_SELL", iso(0)))
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        opps = self._scan()
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0].direction, "sell_polymarket")

    def test_candidate_logged_even_when_below_size_multiple_threshold(self):
        self._seed_baseline(size=50.0)
        self.ws.trades["pm-slug"].append(trade(75.0, "ORDER_ACTION_BUY", iso(0)))  # 1.5x, well under 10x
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        opps = self._scan()
        self.assertEqual(opps, [])
        row = self.conn.execute("SELECT traded, trade_size_usd FROM candidates ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], 0)
        self.assertAlmostEqual(row[1], 75.0)

    def test_no_opportunity_when_implied_prob_out_of_range(self):
        self._seed_baseline(size=50.0)
        self.ws.trades["pm-slug"].append(trade(600.0, "ORDER_ACTION_BUY", iso(0)))
        self.ws.books["pm-slug"] = pm_book(0.90, 0.91)  # mid 0.905, outside [0.40, 0.60]
        opps = self._scan()
        self.assertEqual(opps, [])

    def test_no_opportunity_when_depth_too_thin(self):
        self._seed_baseline(size=50.0)
        self.ws.trades["pm-slug"].append(trade(600.0, "ORDER_ACTION_BUY", iso(0)))
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51, qty=10)  # $5.10 of depth, well under $500
        opps = self._scan()
        self.assertEqual(opps, [])

    def test_counterfactual_captured_after_elapsed_time(self):
        self._seed_baseline(size=50.0)
        self.ws.trades["pm-slug"].append(trade(600.0, "ORDER_ACTION_BUY", iso(0)))
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self._scan()

        opp_id = self.conn.execute("SELECT id FROM opportunities").fetchone()[0]
        self.conn.execute("UPDATE opportunities SET detected_at = ? WHERE id = ?", (iso(-61), opp_id))
        self.conn.commit()

        self.ws.books["pm-slug"] = pm_book(0.52, 0.53)
        self._scan()

        cf60 = self.conn.execute("SELECT counterfactual_60s FROM opportunities WHERE id = ?", (opp_id,)).fetchone()[0]
        self.assertIsNotNone(cf60)
        self.assertAlmostEqual(cf60, 0.525)


if __name__ == "__main__":
    unittest.main()
