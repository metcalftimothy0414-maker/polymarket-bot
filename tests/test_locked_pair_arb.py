from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import unittest
from decimal import Decimal

from bot.db import SCHEMA
from bot.state import MarketState
from bot.strategies.locked_pair_arb import LockedPairArbStrategy, has_open_pair_position


class FakePolymarketWS:
    def __init__(self) -> None:
        self.books: dict[str, dict] = {}
        self._stale: set[str] = set()

    def is_stale(self, slug: str) -> bool:
        return slug in self._stale


def pm_book(bid, ask):
    return {
        "bids": [{"px": {"value": str(bid)}, "qty": "500"}],
        "offers": [{"px": {"value": str(ask)}, "qty": "500"}],
    }


def k_book(yes_bid, no_bid):
    return {"yes_dollars": [[str(yes_bid), "500"]], "no_dollars": [[str(no_bid), "500"]]}


def _future_iso(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat()


class LockedPairArbStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, created_at, "
            "polymarket_end_date, kalshi_close_date) VALUES (1, 'pm-slug', 'KXTEST-25-YES', 1.0, 1, 'now', ?, ?)",
            (_future_iso(7), _future_iso(7)),
        )
        self.conn.commit()

        self.ws = FakePolymarketWS()
        self.state = MarketState(self.ws)
        self.strategy = LockedPairArbStrategy(
            self.conn, self.state,
            min_abs_edge=Decimal("0.01"), hurdle_annual_return=Decimal("0.25"),
        )

    def _scan(self):
        return asyncio.run(self.strategy.scan())

    def test_opens_a_locked_pair_when_edge_is_large(self):
        # Kalshi YES ask = 1 - 0.70 = 0.30; Polymarket NO ask = 1 - 0.80 = 0.20.
        # Combined cost 0.50 vs. $1 payout -> huge edge, easily clears fees+risk+hurdle.
        self.ws.books["pm-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.28, 0.70))
        evals = self._scan()

        traded = [e for e in evals if e.traded]
        self.assertEqual(len(traded), 1)
        self.assertEqual(traded[0].direction, "buy_kalshi_yes_poly_no")

        row = self.conn.execute(
            "SELECT direction, size, leg_a_venue, leg_b_venue, status FROM pair_positions"
        ).fetchone()
        self.assertEqual(row, ("buy_kalshi_yes_poly_no", row[1], "kalshi", "polymarket", "open"))
        self.assertGreater(row[1], 0)

    def test_logs_every_evaluation_including_rejections(self):
        self.ws.books["pm-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.28, 0.70))
        self._scan()
        rows = self.conn.execute("SELECT direction, traded, binding_constraint FROM pair_evaluations").fetchall()
        self.assertEqual(len(rows), 2)  # both directions logged, even the losing one
        traded_flags = {r[1] for r in rows}
        self.assertIn(0, traded_flags)  # the losing direction (buy_poly_yes_kalshi_no) was rejected
        self.assertIn(1, traded_flags)

    def test_no_edge_never_opens_a_position(self):
        # Symmetric spreads on both venues -> both directions cost > $1.
        self.ws.books["pm-slug"] = pm_book(0.49, 0.51)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.49, 0.49))
        evals = self._scan()
        self.assertTrue(all(not e.traded for e in evals))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pair_positions").fetchone()[0], 0)

    def test_does_not_open_a_second_position_on_the_same_pair(self):
        self.ws.books["pm-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.28, 0.70))
        self._scan()
        self.assertTrue(has_open_pair_position(self.conn, 1))
        self._scan()
        count = self.conn.execute("SELECT COUNT(*) FROM pair_positions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_passing_trade_has_canonical_passed_binding_constraint(self):
        self.ws.books["pm-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.28, 0.70))
        self._scan()
        row = self.conn.execute(
            "SELECT binding_constraint FROM pair_evaluations WHERE traded = 1"
        ).fetchone()
        self.assertEqual(row[0], "passed")

    def test_rejected_trade_has_canonical_below_annual_hurdle_or_edge(self):
        # Tiny but positive edge: clears the book-depth walk but not the
        # decision gate — the raw decide() message ("annual_return ... <
        # hurdle ...") must be bucketed into the canonical vocabulary, not
        # stored as free text.
        strategy = LockedPairArbStrategy(
            self.conn, self.state, min_abs_edge=Decimal("0.001"), hurdle_annual_return=Decimal("50"),
        )
        self.ws.books["pm-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.28, 0.70))
        asyncio.run(strategy.scan())
        rows = {r[0] for r in self.conn.execute("SELECT binding_constraint FROM pair_evaluations WHERE traded = 0").fetchall()}
        self.assertTrue(rows.issubset({"below_annual_hurdle", "below_min_edge", "insufficient_depth"}))

    def test_tier_above_max_reviewable_is_skipped_and_logged(self):
        self.conn.execute("UPDATE pairs SET tier = 4 WHERE id = 1")
        self.conn.commit()
        strategy = LockedPairArbStrategy(self.conn, self.state, max_reviewable_tier=3)
        self.ws.books["pm-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.28, 0.70))
        evals = asyncio.run(strategy.scan())
        self.assertEqual(len(evals), 1)
        self.assertFalse(evals[0].traded)
        self.assertEqual(evals[0].reason, "tier_too_high")
        row = self.conn.execute("SELECT binding_constraint FROM pair_evaluations").fetchone()
        self.assertEqual(row[0], "tier_too_high")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pair_positions").fetchone()[0], 0)

    def test_tier_at_or_below_max_reviewable_is_evaluated_normally(self):
        self.conn.execute("UPDATE pairs SET tier = 3 WHERE id = 1")
        self.conn.commit()
        strategy = LockedPairArbStrategy(self.conn, self.state, max_reviewable_tier=3)
        self.ws.books["pm-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.28, 0.70))
        evals = asyncio.run(strategy.scan())
        self.assertTrue(any(e.traded for e in evals))

    def test_null_tier_is_not_gated(self):
        # Pairs verified before tiering existed have tier=NULL — must not
        # be silently blocked by a feature that post-dates them.
        strategy = LockedPairArbStrategy(self.conn, self.state, max_reviewable_tier=3)
        self.ws.books["pm-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.28, 0.70))
        evals = asyncio.run(strategy.scan())
        self.assertTrue(any(e.traded for e in evals))

    def test_stale_data_is_logged_not_silently_dropped(self):
        # §5: every scan of every verified pair persists an evaluation row,
        # even a rejection — a silent skip would make the rejection log
        # blind to how often staleness is the actual bottleneck.
        self.ws.books["pm-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXTEST-25-YES", k_book(0.28, 0.70))
        self.ws._stale.add("pm-slug")
        evals = self._scan()
        self.assertEqual(len(evals), 1)
        self.assertFalse(evals[0].traded)
        self.assertEqual(evals[0].reason, "stale_book")
        row = self.conn.execute("SELECT binding_constraint FROM pair_evaluations").fetchone()
        self.assertEqual(row[0], "stale_book")

    def test_missing_resolution_dates_skips_the_pair(self):
        self.conn.execute(
            "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, created_at) "
            "VALUES (2, 'no-dates-slug', 'KXNODATE', 1.0, 1, 'now')"
        )
        self.conn.commit()
        self.ws.books["no-dates-slug"] = pm_book(0.80, 0.82)
        self.state.kalshi.update("KXNODATE", k_book(0.28, 0.70))
        evals = self._scan()
        self.assertTrue(all(e.pair_id != 2 for e in evals))


if __name__ == "__main__":
    unittest.main()
