import sqlite3
import unittest

from bot.db import SCHEMA
from bot.matching.matcher import find_candidate_pairs, jaccard, normalize_tokens, store_pairs

PM_MARKETS = [
    {
        "slug": "aec-nba-lal-bos-2026-01-10",
        "question": "Lakers vs. Celtics",
        "description": "If Los Angeles wins, resolves Lakers. If Boston wins, resolves Celtics.",
        "endDate": "2026-01-10T23:00:00Z",
    },
    {
        "slug": "aec-nba-mia-nyk-2026-01-10",
        "question": "Heat vs. Knicks",
        "description": "If Miami wins, resolves Heat. If New York wins, resolves Knicks.",
        "endDate": "2026-01-10T23:00:00Z",
    },
]

KALSHI_MARKETS = [
    {
        "ticker": "KXNBA-26JAN10LALBOS",
        "title": "Lakers vs Celtics winner",
        "subtitle": "",
        "close_time": "2026-01-10T23:30:00Z",
        "rules_primary": "Resolves YES if the Los Angeles Lakers win.",
        "rules_secondary": "",
    },
    {
        # Same date, different game — must NOT match the Lakers/Celtics pair.
        "ticker": "KXNBA-26JAN10MIANYK",
        "title": "Heat vs Knicks winner",
        "subtitle": "",
        "close_time": "2026-01-10T23:00:00Z",
        "rules_primary": "Resolves YES if the Miami Heat win.",
        "rules_secondary": "",
    },
    {
        # Same teams, different date — must NOT match either Polymarket market.
        "ticker": "KXNBA-26FEB01LALBOS",
        "title": "Lakers vs Celtics winner",
        "subtitle": "",
        "close_time": "2026-02-01T23:30:00Z",
        "rules_primary": "Resolves YES if the Los Angeles Lakers win.",
        "rules_secondary": "",
    },
]


class TestNormalizeAndJaccard(unittest.TestCase):
    def test_normalize_strips_stopwords_and_punctuation(self):
        tokens = normalize_tokens("Lakers vs. Celtics: Who will win the game?")
        self.assertEqual(tokens, {"lakers", "celtics"})

    def test_jaccard_empty_sets(self):
        self.assertEqual(jaccard(set(), {"a"}), 0.0)
        self.assertEqual(jaccard(set(), set()), 0.0)

    def test_jaccard_identical(self):
        self.assertEqual(jaccard({"a", "b"}, {"a", "b"}), 1.0)


class TestFindCandidatePairs(unittest.TestCase):
    def test_matches_same_teams_same_date(self):
        proposals = find_candidate_pairs(PM_MARKETS, KALSHI_MARKETS, similarity_threshold=0.5)
        matched = {(p.polymarket_slug, p.kalshi_ticker) for p in proposals}
        self.assertIn(("aec-nba-lal-bos-2026-01-10", "KXNBA-26JAN10LALBOS"), matched)
        self.assertIn(("aec-nba-mia-nyk-2026-01-10", "KXNBA-26JAN10MIANYK"), matched)

    def test_rejects_same_date_different_game(self):
        proposals = find_candidate_pairs(PM_MARKETS, KALSHI_MARKETS, similarity_threshold=0.5)
        matched = {(p.polymarket_slug, p.kalshi_ticker) for p in proposals}
        self.assertNotIn(("aec-nba-lal-bos-2026-01-10", "KXNBA-26JAN10MIANYK"), matched)

    def test_rejects_same_teams_different_date(self):
        proposals = find_candidate_pairs(PM_MARKETS, KALSHI_MARKETS, similarity_threshold=0.5)
        matched = {(p.polymarket_slug, p.kalshi_ticker) for p in proposals}
        self.assertNotIn(("aec-nba-lal-bos-2026-01-10", "KXNBA-26FEB01LALBOS"), matched)

    def test_date_tolerance_widens_matches(self):
        # The Heat/Knicks Kalshi market closes 30 min before the Polymarket
        # equivalent's endDate — still same calendar date so tolerance=0 already
        # matches; bump the Kalshi close_time a day later and confirm tolerance=1 catches it.
        shifted = [dict(KALSHI_MARKETS[0], close_time="2026-01-11T00:30:00Z")]
        no_tolerance = find_candidate_pairs(PM_MARKETS, shifted, similarity_threshold=0.5, date_tolerance_days=0)
        with_tolerance = find_candidate_pairs(PM_MARKETS, shifted, similarity_threshold=0.5, date_tolerance_days=1)
        self.assertEqual(no_tolerance, [])
        self.assertEqual(len(with_tolerance), 1)

    def test_occurrence_datetime_used_over_close_time_postponement_buffer(self):
        # Live bug found 2026-08-09: Kalshi's close_time carries a multi-day
        # postponement-rescheduling buffer past the real game
        # (occurrence_datetime). For a team that plays a multi-game series,
        # that buffer can drift into date_tolerance_days of a LATER,
        # different game against the same opponent on the other venue —
        # a false match, not a rescheduling edge case.
        same_day_but_wrong_game = [dict(
            KALSHI_MARKETS[0],
            occurrence_datetime="2026-01-08T23:00:00Z",  # the real game: 2 days before the PM listing
            close_time="2026-01-10T23:30:00Z",  # postponement buffer happens to land on the PM game's date
        )]
        matched_at_zero_tolerance = find_candidate_pairs(
            PM_MARKETS, same_day_but_wrong_game, similarity_threshold=0.5, date_tolerance_days=0,
        )
        self.assertEqual(matched_at_zero_tolerance, [])  # true dates are 2 days apart, must not match
        matched_at_two_day_tolerance = find_candidate_pairs(
            PM_MARKETS, same_day_but_wrong_game, similarity_threshold=0.5, date_tolerance_days=2,
        )
        self.assertEqual(len(matched_at_two_day_tolerance), 1)  # widening tolerance still uses the real date

    def test_one_kalshi_ticker_matching_two_polymarket_listings_is_dropped(self):
        # A team plays the same opponent on back-to-back days (a series);
        # a near-midnight-ET game can round to either UTC calendar date.
        # One Kalshi ticker satisfying token+date match against two
        # DIFFERENT Polymarket listings for the same matchup is ambiguous
        # (which game does the ticker actually refer to?) and must be
        # dropped entirely, not guessed at.
        two_games_same_matchup = [
            dict(PM_MARKETS[0], slug="aec-nba-lal-bos-2026-01-10-a"),
            dict(PM_MARKETS[0], slug="aec-nba-lal-bos-2026-01-10-b"),
        ]
        proposals = find_candidate_pairs(two_games_same_matchup, KALSHI_MARKETS, similarity_threshold=0.5)
        matched_tickers = {p.kalshi_ticker for p in proposals}
        self.assertNotIn("KXNBA-26JAN10LALBOS", matched_tickers)

    def test_unambiguous_matches_survive_the_ambiguity_filter(self):
        proposals = find_candidate_pairs(PM_MARKETS, KALSHI_MARKETS, similarity_threshold=0.5)
        matched = {(p.polymarket_slug, p.kalshi_ticker) for p in proposals}
        self.assertIn(("aec-nba-lal-bos-2026-01-10", "KXNBA-26JAN10LALBOS"), matched)
        self.assertIn(("aec-nba-mia-nyk-2026-01-10", "KXNBA-26JAN10MIANYK"), matched)


class TestStorePairs(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_new_pairs_default_unverified(self):
        proposals = find_candidate_pairs(PM_MARKETS, KALSHI_MARKETS, similarity_threshold=0.5)
        inserted = store_pairs(self.conn, proposals)
        self.assertEqual(inserted, len(proposals))
        row = self.conn.execute("SELECT verified FROM pairs LIMIT 1").fetchone()
        self.assertEqual(row[0], 0)

    def test_duplicate_scan_does_not_reinsert(self):
        proposals = find_candidate_pairs(PM_MARKETS, KALSHI_MARKETS, similarity_threshold=0.5)
        store_pairs(self.conn, proposals)
        second_insert_count = store_pairs(self.conn, proposals)
        self.assertEqual(second_insert_count, 0)

    def test_verified_pair_survives_rescan(self):
        proposals = find_candidate_pairs(PM_MARKETS, KALSHI_MARKETS, similarity_threshold=0.5)
        store_pairs(self.conn, proposals)
        self.conn.execute("UPDATE pairs SET verified = 1 WHERE polymarket_slug = ?", (PM_MARKETS[0]["slug"],))
        self.conn.commit()
        store_pairs(self.conn, proposals)  # rescanning must not reset verified back to 0
        row = self.conn.execute(
            "SELECT verified FROM pairs WHERE polymarket_slug = ?", (PM_MARKETS[0]["slug"],)
        ).fetchone()
        self.assertEqual(row[0], 1)


if __name__ == "__main__":
    unittest.main()
