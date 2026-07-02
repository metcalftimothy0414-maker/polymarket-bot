import asyncio
import sqlite3
import unittest

from bot.db import SCHEMA
from bot.state import MarketState
from bot.strategies.divergence import DivergenceStrategy


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


class TestDivergenceStrategy(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, created_at) "
            "VALUES (1, 'pm-slug', 'K-TICKER', 1.0, 1, 'now')"
        )
        self.conn.commit()

        self.ws = FakePolymarketWS()
        self.state = MarketState(self.ws)
        self.strategy = DivergenceStrategy(self.conn, self.state, entry_threshold_cents=4.0)

    def _scan(self):
        return asyncio.run(self.strategy.scan())

    def test_no_opportunity_when_below_threshold(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.kalshi.update("K-TICKER", k_book(0.505, 0.495))  # kalshi mid ~0.505, tiny edge
        opps = self._scan()
        self.assertEqual(opps, [])
        row = self.conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()
        self.assertEqual(row[0], 0)

    def test_opens_opportunity_when_threshold_crossed(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.kalshi.update("K-TICKER", k_book(0.58, 0.42))  # kalshi mid 0.58 vs pm ask 0.51 -> big edge
        opps = self._scan()
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0].direction, "buy_polymarket")
        row = self.conn.execute("SELECT status FROM opportunities").fetchone()
        self.assertEqual(row[0], "open")

    def test_persists_without_duplicate_row_while_still_open(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.kalshi.update("K-TICKER", k_book(0.58, 0.42))
        self._scan()
        self._scan()
        self._scan()
        row = self.conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()
        self.assertEqual(row[0], 1)  # still just one row, not three

    def test_closes_when_divergence_drops_below_threshold(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.kalshi.update("K-TICKER", k_book(0.58, 0.42))
        self._scan()
        self.state.kalshi.update("K-TICKER", k_book(0.505, 0.495))  # divergence closes
        self._scan()
        row = self.conn.execute("SELECT status, persistence_seconds FROM opportunities").fetchone()
        self.assertEqual(row[0], "closed")
        self.assertIsNotNone(row[1])

    def test_stale_data_never_opens_an_opportunity(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.kalshi.update("K-TICKER", k_book(0.58, 0.42))
        self.ws._stale.add("pm-slug")
        opps = self._scan()
        self.assertEqual(opps, [])
        row = self.conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()
        self.assertEqual(row[0], 0)

    def test_unverified_pair_is_never_scanned(self):
        self.conn.execute(
            "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, created_at) "
            "VALUES (2, 'unverified-slug', 'K-OTHER', 0.9, 0, 'now')"
        )
        self.conn.commit()
        self.ws.books["unverified-slug"] = pm_book(0.10, 0.11)
        self.state.kalshi.update("K-OTHER", k_book(0.90, 0.10))  # huge divergence, but unverified
        opps = self._scan()
        self.assertTrue(all(o.market_ref != "unverified-slug" for o in opps))


if __name__ == "__main__":
    unittest.main()
