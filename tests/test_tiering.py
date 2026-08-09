from __future__ import annotations

import unittest

from bot.matching.normalizers import NormalizedMarket
from bot.matching.tiering import assign_tier


def nm(text: str = "", extra: dict | None = None) -> NormalizedMarket:
    return NormalizedMarket(tokens=set(), date="2026-08-12", text=text, extra=extra or {})


class AssignTierTests(unittest.TestCase):
    def test_clean_pair_stays_tier_1(self):
        pm = nm("Team A vs Team B, resolves per ESPN ET, cancellation or postponement resolves 50-50")
        k = nm("Team A vs Team B winner ET, cancellation or postponement resolves 50-50")
        result = assign_tier(category="sports", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertEqual(result.tier, 1)
        self.assertEqual(result.reasons, [])

    def test_generic_category_forces_tier_5(self):
        pm = nm("x ET postpone")
        k = nm("x ET postpone")
        result = assign_tier(category="generic", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertEqual(result.tier, 5)
        self.assertIn("matched_by_generic_normalizer", result.reasons)

    def test_discretionary_language_raises_tier(self):
        pm = nm("Resolves at the Exchange's discretion. ET. postpone.")
        k = nm("Resolves per rules. ET. postpone.")
        result = assign_tier(category="sports", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertIn("discretionary_language", result.reasons)
        self.assertGreaterEqual(result.tier, 4)

    def test_unnamed_official_source_raises_tier(self):
        pm = nm("Resolves per the official result. ET. postpone.")
        k = nm("Resolves per rules. ET. postpone.")
        result = assign_tier(category="sports", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertIn("unnamed_official_source", result.reasons)

    def test_named_official_source_does_not_flag(self):
        pm = nm("Resolves per the official source of ESPN. ET. postpone.")
        k = nm("Resolves per rules. ET. postpone.")
        result = assign_tier(category="sports", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertNotIn("unnamed_official_source", result.reasons)

    def test_missing_timezone_raises_tier(self):
        pm = nm("Resolves per rules. postpone.")  # no ET/UTC/etc
        k = nm("Resolves per rules. ET. postpone.")
        result = assign_tier(category="sports", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertIn("missing_explicit_timezone", result.reasons)

    def test_missing_cancellation_handling_raises_tier(self):
        pm = nm("Resolves per rules. ET.")  # no cancel/postpone/void/tie
        k = nm("Resolves per rules. ET. postpone.")
        result = assign_tier(category="sports", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertIn("missing_cancellation_postponement_handling", result.reasons)

    def test_different_named_sources_raises_tier(self):
        pm = nm("x ET postpone", extra={"named_source": "CoinDesk"})
        k = nm("x ET postpone", extra={"named_source": "Bloomberg"})
        result = assign_tier(category="numeric_threshold", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertTrue(any(r.startswith("different_named_sources") for r in result.reasons))
        self.assertGreaterEqual(result.tier, 4)

    def test_same_named_source_does_not_flag(self):
        pm = nm("x ET postpone", extra={"named_source": "CoinDesk"})
        k = nm("x ET postpone", extra={"named_source": "coindesk"})  # case-insensitive match
        result = assign_tier(category="numeric_threshold", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertFalse(any(r.startswith("different_named_sources") for r in result.reasons))

    def test_comparison_operator_mismatch_raises_tier(self):
        pm = nm("x ET postpone", extra={"comparison_operator": ">="})
        k = nm("x ET postpone", extra={"comparison_operator": ">"})
        result = assign_tier(category="numeric_threshold", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertTrue(any(r.startswith("comparison_operator_mismatch") for r in result.reasons))

    def test_missing_revision_handling_raises_tier_for_econ_only(self):
        pm = nm("x ET postpone", extra={"mentions_revision_handling": False})
        k = nm("x ET postpone", extra={"mentions_revision_handling": False})
        result = assign_tier(category="economic_indicator", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertIn("missing_revision_handling", result.reasons)
        self.assertGreaterEqual(result.tier, 4)

        # Same missing signal, but not an econ market -> not checked at all.
        result_sports = assign_tier(category="sports", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertNotIn("missing_revision_handling", result_sports.reasons)

    def test_reference_period_mismatch_raises_tier(self):
        pm = nm("x ET postpone", extra={"reference_period": "jul-2026"})
        k = nm("x ET postpone", extra={"reference_period": "aug-2026"})
        result = assign_tier(category="economic_indicator", pm=pm, kalshi=k, days_to_resolution=1)
        self.assertTrue(any(r.startswith("reference_period_mismatch") for r in result.reasons))

    def test_long_dated_market_raises_tier(self):
        pm = nm("x ET postpone")
        k = nm("x ET postpone")
        result = assign_tier(category="sports", pm=pm, kalshi=k, days_to_resolution=200)
        self.assertTrue(any(r.startswith("time_to_resolution_over") for r in result.reasons))

    def test_short_dated_market_does_not_flag(self):
        pm = nm("x ET postpone")
        k = nm("x ET postpone")
        result = assign_tier(category="sports", pm=pm, kalshi=k, days_to_resolution=5)
        self.assertFalse(any(r.startswith("time_to_resolution_over") for r in result.reasons))

    def test_tier_never_exceeds_5(self):
        pm = nm("Resolves at the Exchange's discretion, per the official result.", extra={"named_source": "A", "comparison_operator": ">="})
        k = nm("Resolves per rules.", extra={"named_source": "B", "comparison_operator": ">"})
        result = assign_tier(category="generic", pm=pm, kalshi=k, days_to_resolution=400)
        self.assertEqual(result.tier, 5)


if __name__ == "__main__":
    unittest.main()
