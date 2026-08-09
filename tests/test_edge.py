from __future__ import annotations

import unittest
from decimal import Decimal

from bot.edge import (
    BookLevel,
    Decision,
    RiskParams,
    annualized_return,
    decide,
    executable_depth,
    kalshi_fee,
    polymarket_fee,
    polymarket_multi_fill_taker_fee,
    risk_adjusted_edge,
)


class KalshiFeeTableTests(unittest.TestCase):
    def test_taker_peak_at_50_cents_per_100_contracts(self):
        fee = kalshi_fee(Decimal("0.50"), 100, maker=False)
        self.assertEqual(fee, Decimal("1.7500"))

    def test_taker_collapses_toward_extremes(self):
        fee = kalshi_fee(Decimal("0.01"), 100, maker=False)
        self.assertEqual(fee, Decimal("0.0693"))

    def test_maker_default_multiplier_is_zero(self):
        # M_maker default 0 unless a series override says otherwise.
        fee = kalshi_fee(Decimal("0.50"), 100, maker=True)
        self.assertEqual(fee, Decimal("0"))

    def test_maker_with_series_override_m_equals_1(self):
        from bot import edge

        edge.KALSHI_SERIES_MULTIPLIERS["TESTSERIES"] = (Decimal(1), Decimal(1))
        try:
            fee = kalshi_fee(Decimal("0.50"), 100, maker=True, series="TESTSERIES")
            self.assertEqual(fee, Decimal("0.4375"))
        finally:
            del edge.KALSHI_SERIES_MULTIPLIERS["TESTSERIES"]

    def test_zero_fee_series_override(self):
        from bot import edge

        edge.KALSHI_SERIES_MULTIPLIERS["ZEROFEE"] = (Decimal(0), Decimal(0))
        try:
            self.assertEqual(kalshi_fee(Decimal("0.50"), 100, maker=False, series="ZEROFEE"), Decimal("0"))
        finally:
            del edge.KALSHI_SERIES_MULTIPLIERS["ZEROFEE"]

    def test_rounds_up_not_to_nearest(self):
        # raw = 0.07 * 1 * 0.33 * 0.67 = 0.015477 -> rounds UP to 0.0155, not
        # down/nearest to 0.0154.
        fee = kalshi_fee(Decimal("0.33"), 1, maker=False)
        self.assertEqual(fee, Decimal("0.0155"))

    def test_symmetric_about_50_cents(self):
        self.assertEqual(
            kalshi_fee(Decimal("0.20"), 10, maker=False),
            kalshi_fee(Decimal("0.80"), 10, maker=False),
        )


class PolymarketFeeTableTests(unittest.TestCase):
    def test_taker_ceiling_at_50_cents_per_100_contracts(self):
        fee = polymarket_fee(Decimal("0.50"), 100, maker=False)
        self.assertEqual(fee, Decimal("1.50"))

    def test_maker_rebate_at_50_cents_per_100_contracts(self):
        rebate = polymarket_fee(Decimal("0.50"), 100, maker=True)
        self.assertEqual(rebate, Decimal("-0.31"))

    def test_bankers_rounding_half_to_even(self):
        # raw = 0.0125 * 40 * 0.5 * 0.5 = 0.125 exactly -> equidistant between
        # 0.12 and 0.13 -> rounds to the even neighbor, 0.12 (a plain
        # round-half-up implementation would wrongly give 0.13).
        fee = polymarket_fee(Decimal("0.50"), 40, maker=True)
        self.assertEqual(fee, Decimal("-0.12"))

    def test_symmetric_about_50_cents(self):
        self.assertEqual(
            polymarket_fee(Decimal("0.20"), 10, maker=False),
            polymarket_fee(Decimal("0.80"), 10, maker=False),
        )

    def test_multi_fill_cap_never_exceeds_cumulative_rounded_exact(self):
        # Two fills whose independently-rounded fees could sum above the
        # cumulative exact fee's rounding; total must be capped.
        fills = [(Decimal("0.50"), 1), (Decimal("0.50"), 1)]
        total = polymarket_multi_fill_taker_fee(fills)
        exact_total = sum(Decimal("0.06") * p * (1 - p) for p, _c in fills)
        cap = exact_total.quantize(Decimal("0.01"))
        self.assertLessEqual(total, cap)

    def test_multi_fill_empty(self):
        self.assertEqual(polymarket_multi_fill_taker_fee([]), Decimal(0))


class HurdleRateTableTests(unittest.TestCase):
    """Reproduces the build prompt's §3.5 round-trip cost table exactly."""

    def _cost_cents(self, p: Decimal, *, kalshi_maker: bool, kalshi_m: Decimal, poly_maker: bool) -> Decimal:
        # C=100 to match the docs' own worked examples exactly (a single
        # contract hits real rounding noise the table's continuous-rate
        # illustration doesn't model) — a $X fee on 100 contracts is
        # numerically X cents per contract, so no further scaling needed.
        from bot import edge

        edge.KALSHI_SERIES_MULTIPLIERS["_HURDLE_TEST"] = (kalshi_m, kalshi_m)
        try:
            k_fee = kalshi_fee(p, 100, maker=kalshi_maker, series="_HURDLE_TEST")
        finally:
            del edge.KALSHI_SERIES_MULTIPLIERS["_HURDLE_TEST"]
        p_fee = polymarket_fee(p, 100, maker=poly_maker)
        return k_fee + p_fee

    def test_take_both_legs(self):
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.50"), kalshi_maker=False, kalshi_m=Decimal(1), poly_maker=False)), 3.25, places=2)
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.90"), kalshi_maker=False, kalshi_m=Decimal(1), poly_maker=False)), 1.17, places=2)

    def test_kalshi_maker_m1_plus_poly_take(self):
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.50"), kalshi_maker=True, kalshi_m=Decimal(1), poly_maker=False)), 1.94, places=2)
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.90"), kalshi_maker=True, kalshi_m=Decimal(1), poly_maker=False)), 0.70, places=2)

    def test_kalshi_take_plus_poly_maker(self):
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.50"), kalshi_maker=False, kalshi_m=Decimal(1), poly_maker=True)), 1.44, places=2)
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.90"), kalshi_maker=False, kalshi_m=Decimal(1), poly_maker=True)), 0.52, places=2)

    def test_dual_maker_m1(self):
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.50"), kalshi_maker=True, kalshi_m=Decimal(1), poly_maker=True)), 0.13, places=2)
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.90"), kalshi_maker=True, kalshi_m=Decimal(1), poly_maker=True)), 0.05, places=2)

    def test_dual_maker_m0_gets_paid(self):
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.50"), kalshi_maker=True, kalshi_m=Decimal(0), poly_maker=True)), -0.31, places=2)
        self.assertAlmostEqual(float(self._cost_cents(Decimal("0.90"), kalshi_maker=True, kalshi_m=Decimal(0), poly_maker=True)), -0.11, places=2)


def _taker_fee_a(price: Decimal, contracts: int) -> Decimal:
    return kalshi_fee(price, contracts, maker=False)


def _taker_fee_b(price: Decimal, contracts: int) -> Decimal:
    return polymarket_fee(price, contracts, maker=False)


class BookWalkerTests(unittest.TestCase):
    def test_empty_book_returns_zero_never_raises(self):
        result = executable_depth([], [], _taker_fee_a, _taker_fee_b, Decimal("0.01"))
        self.assertEqual(result.size, 0)
        self.assertIsNone(result.vwap_a)

    def test_never_exceeds_available_depth(self):
        levels_a = [BookLevel(Decimal("0.40"), 3)]
        levels_b = [BookLevel(Decimal("0.40"), 5)]
        result = executable_depth(levels_a, levels_b, _taker_fee_a, _taker_fee_b, Decimal("-1"))
        self.assertLessEqual(result.size, 3)  # bounded by the thinner side

    def test_stops_at_min_edge_threshold(self):
        # 0.40 + 0.40 = 0.80 total cost before fees -> 20c gross edge, easily
        # clears a demanding 15c min edge; second level is worse and should
        # not be taken once fees push it under threshold.
        levels_a = [BookLevel(Decimal("0.40"), 100)]
        levels_b = [BookLevel(Decimal("0.40"), 100)]
        result = executable_depth(levels_a, levels_b, _taker_fee_a, _taker_fee_b, Decimal("0.15"))
        self.assertGreater(result.size, 0)
        self.assertGreaterEqual(result.net_edge_per_contract, Decimal("0.15"))

    def test_no_edge_returns_zero_size(self):
        levels_a = [BookLevel(Decimal("0.60"), 10)]
        levels_b = [BookLevel(Decimal("0.60"), 10)]  # 1.20 > 1.00, never profitable
        result = executable_depth(levels_a, levels_b, _taker_fee_a, _taker_fee_b, Decimal("0.01"))
        self.assertEqual(result.size, 0)


class RiskAdjustedEdgeTests(unittest.TestCase):
    def test_matches_worked_example_1_7_cents(self):
        # "at a 2% divergence probability with 0.85 asymmetry, the risk
        # charge is 1.7c per contract" (build prompt §5.3).
        risk = RiskParams(p_divergence=Decimal("0.02"), asymmetry=Decimal("0.85"))
        adjusted = risk_adjusted_edge(Decimal("0.05"), risk)
        charge = Decimal("0.05") - adjusted
        self.assertEqual(charge, Decimal("0.017"))

    def test_can_flip_a_positive_net_edge_negative(self):
        risk = RiskParams(p_divergence=Decimal("0.05"), asymmetry=Decimal("1.0"))
        adjusted = risk_adjusted_edge(Decimal("0.025"), risk)
        self.assertLess(adjusted, 0)


class DecisionRuleTests(unittest.TestCase):
    def test_rejects_below_min_abs_edge(self):
        d = decide(Decimal("0.005"), Decimal("1.0"), 10)
        self.assertFalse(d.should_trade)
        self.assertIn("min_abs_edge", d.reason)

    def test_rejects_below_hurdle(self):
        d = decide(Decimal("0.02"), Decimal("0.10"), 10)
        self.assertFalse(d.should_trade)
        self.assertIn("hurdle", d.reason)

    def test_accepts_when_all_pass(self):
        d = decide(Decimal("0.02"), Decimal("0.30"), 10)
        self.assertTrue(d.should_trade)


class AnnualizedReturnTests(unittest.TestCase):
    def test_zero_capital_or_time_is_zero(self):
        self.assertEqual(annualized_return(Decimal("0.02"), Decimal(0), Decimal(5)), Decimal(0))
        self.assertEqual(annualized_return(Decimal("0.02"), Decimal("0.50"), Decimal(0)), Decimal(0))

    def test_higher_edge_gives_higher_return(self):
        low = annualized_return(Decimal("0.01"), Decimal("0.50"), Decimal(5))
        high = annualized_return(Decimal("0.05"), Decimal("0.50"), Decimal(5))
        self.assertGreater(high, low)


if __name__ == "__main__":
    unittest.main()
