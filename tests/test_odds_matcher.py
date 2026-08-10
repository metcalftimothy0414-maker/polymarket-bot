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
    def test_uses_game_start_time_not_end_date_for_matching(self):
        # Real Polymarket US markets: endDate is a resolution deadline that can be
        # ~2 weeks after the actual event; gameStartTime is the real event date.
        # Matching on endDate would silently never align with another venue's
        # commence_time/close_time.
        pm_market = {
            "slug": "aec-ufc-fighter1-fighter2-2026-07-11",
            "question": "Fighter One vs. Fighter Two",
            "description": "",
            "endDate": "2026-07-25T17:00:00Z",  # two weeks after the fight
            "gameStartTime": "2026-07-11T21:00:00Z",  # the actual fight date
            "marketSides": [
                {"long": True, "team": {"name": "Fighter One"}},
                {"long": False, "team": {"name": "Fighter Two"}},
            ],
        }
        odds_game = {
            "id": "game-fight",
            "sport_key": "mma_mixed_martial_arts",
            "home_team": "Fighter One",
            "away_team": "Fighter Two",
            "commence_time": "2026-07-11T21:00:00Z",  # matches gameStartTime, not endDate
        }
        proposals = find_odds_api_pairs([pm_market], [odds_game], similarity_threshold=0.5)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].odds_api_game_id, "game-fight")

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

    def test_back_to_back_series_game_is_dropped_not_guessed(self):
        # §5 regression: a team can play the same opponent on consecutive
        # days (a series) — one Odds API game_id must not satisfy the
        # date+token check against more than one distinct Polymarket slug
        # for the same matchup, and vice versa. Mirrors
        # test_matcher.TestFindCandidatePairs.test_one_kalshi_ticker_matching_two_polymarket_listings_is_dropped
        # for the Kalshi mapper — both now share bot.matching.matcher._drop_ambiguous.
        pm_markets = [
            {
                "slug": "aec-nba-lal-bos-2026-01-10",
                "question": "Lakers vs. Celtics",
                "description": "",
                "gameStartTime": "2026-01-10T23:00:00Z",
                "marketSides": [
                    {"long": True, "team": {"name": "Los Angeles Lakers"}},
                    {"long": False, "team": {"name": "Boston Celtics"}},
                ],
            },
            {
                # Same matchup, second game of a back-to-back series, close
                # enough with date_tolerance_days that both PM listings are
                # within tolerance of the one Odds API game below.
                "slug": "aec-nba-lal-bos-2026-01-11",
                "question": "Lakers vs. Celtics",
                "description": "",
                "gameStartTime": "2026-01-11T23:00:00Z",
                "marketSides": [
                    {"long": True, "team": {"name": "Los Angeles Lakers"}},
                    {"long": False, "team": {"name": "Boston Celtics"}},
                ],
            },
        ]
        odds_games = [
            {
                "id": "game-lal-bos-series",
                "sport_key": "basketball_nba",
                "home_team": "Los Angeles Lakers",
                "away_team": "Boston Celtics",
                "commence_time": "2026-01-10T23:30:00Z",
            },
        ]
        proposals = find_odds_api_pairs(pm_markets, odds_games, similarity_threshold=0.5, date_tolerance_days=1)
        self.assertEqual(proposals, [])


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
