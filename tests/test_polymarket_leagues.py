from __future__ import annotations

import unittest

from bot.feeds.polymarket import filter_by_leagues, league_of


class TestLeagueOf(unittest.TestCase):
    def test_extracts_league_from_slug_second_segment(self):
        self.assertEqual(league_of({"slug": "tec-mlb-nlchamp-2026-09-27-nym"}), "mlb")
        self.assertEqual(league_of({"slug": "aec-ufc-padpim-bensai-2026-07-11"}), "ufc")

    def test_none_for_malformed_slug(self):
        self.assertIsNone(league_of({"slug": "onesegment"}))
        self.assertIsNone(league_of({"slug": ""}))
        self.assertIsNone(league_of({}))


class TestFilterByLeagues(unittest.TestCase):
    def test_empty_leagues_list_returns_everything(self):
        markets = [{"slug": "tec-mlb-x"}, {"slug": "aec-ufc-y"}]
        self.assertEqual(filter_by_leagues(markets, []), markets)

    def test_keeps_only_configured_leagues_case_insensitively(self):
        markets = [
            {"slug": "tec-mlb-nlchamp-nym"},
            {"slug": "aec-nba-lal-bos"},
            {"slug": "aec-ufc-padpim-bensai"},
        ]
        result = filter_by_leagues(markets, ["MLB", "ufc"])
        self.assertEqual([m["slug"] for m in result], ["tec-mlb-nlchamp-nym", "aec-ufc-padpim-bensai"])


if __name__ == "__main__":
    unittest.main()
