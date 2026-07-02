import sqlite3
import unittest

from bot.db import SCHEMA
from bot.matching.matcher import find_odds_api_pairs, store_odds_pairs

PM_MARKETS = [
    {
        "slug": "aec-nba-lal-bos-2026-01-10",
        "question": "Lakers vs. Celtics",
        "description": "If Los Angeles wins, resolves Lakers.",
        "endDate": "2026-01-10T23:00:00Z",
        "marketSides": [
            {"long": True, "team": {"name": "Los Angeles Lakers"}},
            {"long": False, "team": {"name": "Boston Celtics"}},
        ],
    },
    {
        "slug": "aec-nba-mia-nyk-2026-01-10",
        "question": "Heat vs. Knicks",
        "description": "If Miami wins, resolves Heat.",
        "endDate": "2026-01-10T23:00:00Z",
        "marketSides": [
            {"long": True, "team": {"name": "Miami Heat"}},
            {"long": False, "team": {"name": "New York Knicks"}},
        ],
    },
    {
        # No 'long' side flagged at all — must be skipped, not crash.
        "slug": "aec-nba-no-long-flag",
        "question": "Suns vs. Nets",
        "description": "",
        "endDate": "2026-01-10T23:00:00Z",
        "marketSides": [
            {"long": False, "team": {"name": "Phoenix Suns"}},
            {"long": False, "team": {"name": "Brooklyn Nets"}},
        ],
    },
]

ODDS_GAMES = [
    {
        "id": "game-lal-bos",
        "sport_key": "basketball_nba",
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-01-10T23:30:00Z",
    },
    {
        "id": "game-mia-nyk",
        "sport_key": "basketball_nba",
        "home_team": "Miami Heat",
        "away_team": "New York Knicks",
        "commence_time": "2026-01-10T23:00:00Z",
    },
    {
        "id": "game-lal-bos-later",
        "sport_key": "basketball_nba",
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-02-01T23:30:00Z",
    },
]


class TestFindOddsApiPairs(unittest.TestCase):
    def test_matches_same_teams_same_date_and_captures_long_team(self):
        proposals = find_odds_api_pairs(PM_MARKETS, ODDS_GAMES, similarity_threshold=0.5)
        match = next(p for p in proposals if p.polymarket_slug == "aec-nba-lal-bos-2026-01-10" and p.odds_api_game_id == "game-lal-bos")
        self.assertEqual(match.long_team, "Los Angeles Lakers")

    def test_rejects_same_date_different_game(self):
        proposals = find_odds_api_pairs(PM_MARKETS, ODDS_GAMES, similarity_threshold=0.5)
        matched = {(p.polymarket_slug, p.odds_api_game_id) for p in proposals}
        self.assertNotIn(("aec-nba-lal-bos-2026-01-10", "game-mia-nyk"), matched)

    def test_rejects_same_teams_different_date(self):
        proposals = find_odds_api_pairs(PM_MARKETS, ODDS_GAMES, similarity_threshold=0.5)
        matched = {(p.polymarket_slug, p.odds_api_game_id) for p in proposals}
        self.assertNotIn(("aec-nba-lal-bos-2026-01-10", "game-lal-bos-later"), matched)

    def test_market_with_no_long_side_is_skipped_not_crashed(self):
        proposals = find_odds_api_pairs(PM_MARKETS, ODDS_GAMES, similarity_threshold=0.5)
        self.assertTrue(all(p.polymarket_slug != "aec-nba-no-long-flag" for p in proposals))


class TestStoreOddsPairs(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_new_pairs_default_unverified(self):
        proposals = find_odds_api_pairs(PM_MARKETS, ODDS_GAMES, similarity_threshold=0.5)
        inserted = store_odds_pairs(self.conn, proposals)
        self.assertEqual(inserted, len(proposals))
        row = self.conn.execute("SELECT verified FROM odds_pairs LIMIT 1").fetchone()
        self.assertEqual(row[0], 0)

    def test_duplicate_scan_does_not_reinsert(self):
        proposals = find_odds_api_pairs(PM_MARKETS, ODDS_GAMES, similarity_threshold=0.5)
        store_odds_pairs(self.conn, proposals)
        second = store_odds_pairs(self.conn, proposals)
        self.assertEqual(second, 0)


if __name__ == "__main__":
    unittest.main()
