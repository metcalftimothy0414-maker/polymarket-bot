import unittest

from bot.fees import maker_fee, taker_fee
from bot.pricing import kalshi_best_yes_bid_ask, kalshi_mid_price, polymarket_best_bid_ask, polymarket_mid_price


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


if __name__ == "__main__":
    unittest.main()
