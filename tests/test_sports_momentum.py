from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import time
import unittest

from bot.db import SCHEMA
from bot.state import MarketState
from bot.strategies.sports_momentum import SportsMomentumStrategy


class FakePolymarketWS:
    def __init__(self) -> None:
        self.books: dict[str, dict] = {}
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


class TestSportsMomentumStrategy(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO markets (slug, question, category, end_date, game_start_time, active, closed, "
            "raw_json, discovered_at, updated_at) VALUES (?, 'q', 'sports', ?, ?, 1, 0, '', 'now', 'now')",
            ("pm-slug", iso(1800), iso(-600)),  # started 10 min ago, ends in 30 min
        )
        self.conn.commit()

        self.ws = FakePolymarketWS()
        self.state = MarketState(self.ws)
        self.strategy = SportsMomentumStrategy(self.conn, self.state, market_slugs=["pm-slug"])

    def _seed_history(self, mid_then: float, lookback_ago: float | None = None):
        lookback = lookback_ago if lookback_ago is not None else self.strategy.momentum_lookback_seconds + 1
        self.strategy._price_history["pm-slug"] = [(time.monotonic() - lookback, mid_then)]

    def _scan(self):
        return asyncio.run(self.strategy.scan())

    def test_no_signal_without_price_history(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        opps = self._scan()
        self.assertEqual(opps, [])
        candidates = self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        self.assertEqual(candidates, 0)  # no momentum reading yet -> nothing to log

    def test_opens_opportunity_on_positive_momentum_with_filters_passing(self):
        self._seed_history(0.47)
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)  # mid 0.505, momentum ~3.5c >= 3c threshold
        opps = self._scan()
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0].direction, "buy_polymarket")

    def test_candidate_logged_even_when_momentum_below_threshold(self):
        self._seed_history(0.503)  # momentum ~0.2c, well under 3c threshold
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        opps = self._scan()
        self.assertEqual(opps, [])
        row = self.conn.execute("SELECT traded FROM candidates").fetchone()
        self.assertEqual(row[0], 0)

    def test_no_opportunity_when_implied_prob_out_of_range(self):
        self._seed_history(0.85)
        self.ws.books["pm-slug"] = pm_book(0.90, 0.91)  # big momentum but mid 0.905 outside [0.40,0.60]
        opps = self._scan()
        self.assertEqual(opps, [])

    def test_no_opportunity_when_spread_too_wide(self):
        self._seed_history(0.45)
        self.ws.books["pm-slug"] = pm_book(0.48, 0.55)  # spread 7c > max 3c
        opps = self._scan()
        self.assertEqual(opps, [])

    def test_no_opportunity_when_depth_insufficient(self):
        self._seed_history(0.47)
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51, qty=10)  # depth ~5.1 << $500
        opps = self._scan()
        self.assertEqual(opps, [])

    def test_no_opportunity_when_game_not_in_progress(self):
        self.conn.execute(
            "UPDATE markets SET game_start_time = ? WHERE slug = 'pm-slug'", (iso(600),)  # starts in 10 min
        )
        self.conn.commit()
        self._seed_history(0.47)
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        opps = self._scan()
        self.assertEqual(opps, [])

    def test_persists_without_duplicate_row(self):
        self._seed_history(0.47)
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self._scan()
        self._seed_history(0.47, lookback_ago=self.strategy.momentum_lookback_seconds + 1)
        self._scan()
        row = self.conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()
        self.assertEqual(row[0], 1)

    def test_counterfactual_captured_after_elapsed_time(self):
        self._seed_history(0.47)
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self._scan()

        opp_id = self.conn.execute("SELECT id FROM opportunities").fetchone()[0]
        # backdate detected_at as if 61 seconds have already passed
        self.conn.execute(
            "UPDATE opportunities SET detected_at = ? WHERE id = ?", (iso(-61), opp_id)
        )
        self.conn.commit()

        self.ws.books["pm-slug"] = pm_book(0.52, 0.53)  # price moved since entry
        self._seed_history(0.47)  # re-seed so momentum still computes this tick
        self._scan()

        cf60 = self.conn.execute("SELECT counterfactual_60s FROM opportunities WHERE id = ?", (opp_id,)).fetchone()[0]
        self.assertIsNotNone(cf60)
        self.assertAlmostEqual(cf60, 0.525)


if __name__ == "__main__":
    unittest.main()
