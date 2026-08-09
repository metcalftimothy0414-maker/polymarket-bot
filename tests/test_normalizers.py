from __future__ import annotations

import unittest

from bot.matching.matcher import find_candidate_pairs_by_category
from bot.matching.normalizers import (
    EconomicIndicatorNormalizer,
    GenericNormalizer,
    NumericThresholdNormalizer,
    PoliticsElectionsNormalizer,
    normalizer_for,
)


class NormalizerForTests(unittest.TestCase):
    def test_known_categories_resolve_to_their_normalizer(self):
        self.assertEqual(normalizer_for("economic_indicator").category, "economic_indicator")
        self.assertEqual(normalizer_for("politics_elections").category, "politics_elections")
        self.assertEqual(normalizer_for("numeric_threshold").category, "numeric_threshold")
        self.assertEqual(normalizer_for("sports").category, "sports")

    def test_unknown_category_falls_back_to_generic(self):
        self.assertEqual(normalizer_for("something_new_and_unmapped").category, "generic")


class EconomicIndicatorNormalizerTests(unittest.TestCase):
    def setUp(self):
        self.n = EconomicIndicatorNormalizer()

    def test_extracts_reference_period_and_threshold(self):
        pm = self.n.normalize_polymarket({
            "question": "Will July 2026 CPI come in above 3.2%?",
            "description": "Resolves YES if BLS reports headline CPI above 3.2% for July 2026.",
            "endDate": "2026-08-13T12:30:00Z",
        })
        self.assertEqual(pm.extra["reference_period"], "jul-2026")
        self.assertEqual(pm.extra["threshold_value"], 3.2)
        self.assertFalse(pm.extra["mentions_revision_handling"])

    def test_flags_revision_handling_when_mentioned(self):
        k = self.n.normalize_kalshi({
            "title": "CPI above 3.2% in July?",
            "rules_primary": "Resolves on the initial BLS release; not affected by later revisions.",
            "close_time": "2026-08-13T12:30:00Z",
        })
        self.assertTrue(k.extra["mentions_revision_handling"])

    def test_uses_close_time_directly_no_occurrence_datetime_lookup(self):
        # Unlike sports, econ releases don't have a postponement-buffer gap
        # between close_time and the actual release — close_time IS the date.
        k = self.n.normalize_kalshi({"title": "x", "close_time": "2026-08-13T12:30:00Z"})
        self.assertEqual(k.date, "2026-08-13")


class PoliticsElectionsNormalizerTests(unittest.TestCase):
    def test_extracts_office_tokens(self):
        n = PoliticsElectionsNormalizer()
        pm = n.normalize_polymarket({"question": "Will the Democrat win the Iowa Senate race?", "endDate": "2026-11-03T00:00:00Z"})
        self.assertIn("senate", pm.extra["office"])

    def test_no_office_mentioned_gives_empty_set(self):
        n = PoliticsElectionsNormalizer()
        pm = n.normalize_polymarket({"question": "Random unrelated question", "endDate": "2026-11-03T00:00:00Z"})
        self.assertEqual(pm.extra["office"], set())


class NumericThresholdNormalizerTests(unittest.TestCase):
    def setUp(self):
        self.n = NumericThresholdNormalizer()

    def test_extracts_operator_and_value(self):
        pm = self.n.normalize_polymarket({
            "question": "Will Bitcoin be at least 150000 by year end?",
            "endDate": "2026-12-31T00:00:00Z",
        })
        self.assertEqual(pm.extra["comparison_operator"], ">=")
        self.assertEqual(pm.extra["threshold_value"], 150000.0)

    def test_extracts_named_source(self):
        k = self.n.normalize_kalshi({
            "title": "x", "rules_primary": "Settles according to CoinDesk closing price.",
            "close_time": "2026-12-31T00:00:00Z",
        })
        self.assertEqual(k.extra["named_source"], "CoinDesk")

    def test_no_source_mentioned_is_none(self):
        k = self.n.normalize_kalshi({"title": "x", "close_time": "2026-12-31T00:00:00Z"})
        self.assertIsNone(k.extra["named_source"])


class GenericNormalizerTests(unittest.TestCase):
    def test_no_structured_extraction_just_tokens_and_date(self):
        n = GenericNormalizer()
        pm = n.normalize_polymarket({"question": "Will X happen?", "endDate": "2026-12-31T00:00:00Z"})
        self.assertEqual(pm.extra, {})
        self.assertIn("happen", pm.tokens)
        self.assertEqual(pm.date, "2026-12-31")


class FindCandidatePairsByCategoryTests(unittest.TestCase):
    def test_economic_indicator_end_to_end_match(self):
        pm_markets = [{
            "slug": "cpi-jul-2026", "question": "Will July 2026 CPI print above 3.2%?",
            "description": "", "endDate": "2026-08-13T12:30:00Z",
        }]
        kalshi_markets = [{
            "ticker": "KXCPIYOY-26JUL-T3.2", "title": "CPI above 3.2% July 2026?",
            "rules_primary": "", "rules_secondary": "", "close_time": "2026-08-13T12:30:00Z",
        }]
        proposals = find_candidate_pairs_by_category(
            "economic_indicator", pm_markets, kalshi_markets, similarity_threshold=0.3,
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].category, "economic_indicator")

    def test_unmapped_category_uses_generic_normalizer_without_crashing(self):
        pm_markets = [{"slug": "s", "question": "Will thing happen?", "endDate": "2026-12-31T00:00:00Z"}]
        kalshi_markets = [{"ticker": "T", "title": "Will thing happen?", "close_time": "2026-12-31T00:00:00Z"}]
        proposals = find_candidate_pairs_by_category("some_new_category", pm_markets, kalshi_markets, similarity_threshold=0.3)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].category, "some_new_category")


if __name__ == "__main__":
    unittest.main()
