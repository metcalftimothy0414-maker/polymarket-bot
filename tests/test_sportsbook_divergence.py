import asyncio
import sqlite3
import unittest

from bot.db import SCHEMA
from bot.state import MarketState
from bot.strategies.sportsbook_divergence import SportsbookDivergenceStrategy


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


def odds_game(prices: list[tuple[float, float]]):
    return {
        "bookmakers": [
            {"key": f"book{i}", "markets": [{
                "key": "h2h",
                "outcomes": [{"name": "Lakers", "price": lp}, {"name": "Celtics", "price": cp}],
            }]}
            for i, (lp, cp) in enumerate(prices)
        ]
    }


class TestSportsbookDivergenceStrategy(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO odds_pairs (id, polymarket_slug, odds_api_game_id, odds_api_sport_key, long_team, "
            "similarity_score, verified, created_at) "
            "VALUES (1, 'pm-slug', 'game-1', 'basketball_nba', 'Lakers', 1.0, 1, 'now')"
        )
        self.conn.commit()

        self.ws = FakePolymarketWS()
        self.state = MarketState(self.ws)
        self.strategy = SportsbookDivergenceStrategy(self.conn, self.state, entry_threshold_cents=4.0)

    def _scan(self):
        return asyncio.run(self.strategy.scan())

    def test_no_opportunity_when_below_threshold(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.odds_api.update("game-1", odds_game([(1.96, 1.96)]))  # consensus ~0.50, tiny edge vs pm ask 0.51
        opps = self._scan()
        self.assertEqual(opps, [])

    def test_opens_opportunity_when_threshold_crossed(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.odds_api.update("game-1", odds_game([(1.6, 2.6)]))  # consensus prob for Lakers ~0.60 vs pm ask 0.51
        opps = self._scan()
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0].direction, "buy_polymarket")

    def test_persists_without_duplicate_row(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.odds_api.update("game-1", odds_game([(1.6, 2.6)]))
        self._scan()
        self._scan()
        row = self.conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()
        self.assertEqual(row[0], 1)

    def test_closes_when_divergence_drops(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.odds_api.update("game-1", odds_game([(1.6, 2.6)]))
        self._scan()
        self.state.odds_api.update("game-1", odds_game([(1.96, 1.96)]))
        self._scan()
        row = self.conn.execute("SELECT status FROM opportunities").fetchone()
        self.assertEqual(row[0], "closed")

    def test_stale_odds_data_never_opens_opportunity(self):
        self.ws.books["pm-slug"] = pm_book(0.50, 0.51)
        self.state.odds_api.update("game-1", odds_game([(1.6, 2.6)]))
        self.state.odds_api._last_seen["game-1"] -= 120  # force stale beyond ODDS_API_STALE_SECONDS
        opps = self._scan()
        self.assertEqual(opps, [])

    def test_unverified_pair_is_never_scanned(self):
        self.conn.execute(
            "INSERT INTO odds_pairs (id, polymarket_slug, odds_api_game_id, odds_api_sport_key, long_team, "
            "similarity_score, verified, created_at) "
            "VALUES (2, 'unverified-slug', 'game-2', 'basketball_nba', 'Celtics', 0.9, 0, 'now')"
        )
        self.conn.commit()
        self.ws.books["unverified-slug"] = pm_book(0.10, 0.11)
        self.state.odds_api.update("game-2", odds_game([(9.0, 1.11)]))
        opps = self._scan()
        self.assertTrue(all(o.market_ref != "unverified-slug" for o in opps))


if __name__ == "__main__":
    unittest.main()
