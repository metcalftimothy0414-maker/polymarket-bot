import unittest

from bot.fees import maker_fee, taker_fee
from bot.pricing import (
    consensus_devigged_prob_for_team,
    devig_two_way,
    kalshi_best_yes_bid_ask,
    kalshi_mid_price,
    polymarket_best_bid_ask,
    polymarket_mid_price,
)


def pm_book(bid, ask):
    return {
        "bids": [{"px": {"value": str(bid)}, "qty": "100"}],
        "offers": [{"px": {"value": str(ask)}, "qty": "100"}],
    }


class TestPolymarketPricing(unittest.TestCase):
    def test_best_bid_ask(self):
        self.assertEqual(polymarket_best_bid_ask(pm_book(0.45, 0.47)), (0.45, 0.47))

    def test_mid_price(self):
        self.assertAlmostEqual(polymarket_mid_price(pm_book(0.45, 0.47)), 0.46)

    def test_empty_book_returns_none(self):
        self.assertEqual(polymarket_best_bid_ask({"bids": [], "offers": []}), (None, None))
        self.assertIsNone(polymarket_mid_price({"bids": [], "offers": []}))


class TestKalshiPricing(unittest.TestCase):
    def test_yes_ask_is_complement_of_no_bid(self):
        orderbook = {"yes_dollars": [["0.44", "100"]], "no_dollars": [["0.53", "50"]]}
        bid, ask = kalshi_best_yes_bid_ask(orderbook)
        self.assertAlmostEqual(bid, 0.44)
        self.assertAlmostEqual(ask, 0.47)  # 1 - 0.53

    def test_mid_price(self):
        orderbook = {"yes_dollars": [["0.44", "100"]], "no_dollars": [["0.53", "50"]]}
        self.assertAlmostEqual(kalshi_mid_price(orderbook), 0.455)

    def test_missing_side_returns_none(self):
        self.assertIsNone(kalshi_mid_price({"yes_dollars": [], "no_dollars": []}))


class TestFees(unittest.TestCase):
    def test_taker_fee_matches_documented_example(self):
        # docs.polymarket.us/fees.md: taker theta=0.06 -> $1.50/100 contracts at p=$0.50
        self.assertAlmostEqual(taker_fee(0.50), 0.015)

    def test_maker_fee_is_a_rebate(self):
        self.assertLess(maker_fee(0.50), 0)


def game_fixture(bookmaker_prices: list[tuple[float, float]]) -> dict:
    return {
        "bookmakers": [
            {
                "key": f"book{i}",
                "markets": [{
                    "key": "h2h",
                    "outcomes": [{"name": "Lakers", "price": lp}, {"name": "Celtics", "price": cp}],
                }],
            }
            for i, (lp, cp) in enumerate(bookmaker_prices)
        ]
    }


class TestDevig(unittest.TestCase):
    def test_no_vig_case_unchanged(self):
        # 1/1.5 + 1/3.0 == 1.0 exactly, so de-vig should be a no-op
        a, b = devig_two_way(1 / 1.5, 1 / 3.0)
        self.assertAlmostEqual(a, 1 / 1.5)
        self.assertAlmostEqual(b, 1 / 3.0)

    def test_removes_symmetric_vig(self):
        # both sides at decimal 1.9 (~ -110/-110): raw probs sum to 1.0526, should devig to 0.5/0.5
        a, b = devig_two_way(1 / 1.9, 1 / 1.9)
        self.assertAlmostEqual(a, 0.5)
        self.assertAlmostEqual(b, 0.5)

    def test_consensus_uses_median_across_bookmakers(self):
        game = game_fixture([(1.9, 1.9), (2.0, 1.8), (1.8, 2.0)])
        prob = consensus_devigged_prob_for_team(game, "Lakers")
        self.assertIsNotNone(prob)
        self.assertGreater(prob, 0.4)
        self.assertLess(prob, 0.6)

    def test_missing_market_returns_none(self):
        self.assertIsNone(consensus_devigged_prob_for_team({"bookmakers": []}, "Lakers"))


if __name__ == "__main__":
    unittest.main()
