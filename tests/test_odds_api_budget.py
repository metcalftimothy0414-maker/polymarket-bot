from __future__ import annotations

import sqlite3
import unittest

from bot.db import SCHEMA
from bot.odds_api_budget import credits_used_this_period, record_call_cost, within_budget


class OddsApiBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_within_budget_when_nothing_recorded_yet(self):
        self.assertTrue(within_budget(self.conn, monthly_credit_budget=400, cost=1))

    def test_record_call_cost_accumulates_within_a_period(self):
        record_call_cost(self.conn, 1, now="2026-08-09T00:00:00Z")
        record_call_cost(self.conn, 1, now="2026-08-09T04:00:00Z")
        self.assertEqual(credits_used_this_period(self.conn, now="2026-08-09T08:00:00Z"), 2)

    def test_within_budget_becomes_false_once_exhausted(self):
        record_call_cost(self.conn, 400, now="2026-08-09T00:00:00Z")
        self.assertFalse(within_budget(self.conn, monthly_credit_budget=400, cost=1, now="2026-08-09T04:00:00Z"))

    def test_next_call_would_exceed_budget_even_if_current_usage_is_under(self):
        record_call_cost(self.conn, 399, now="2026-08-09T00:00:00Z")
        self.assertFalse(within_budget(self.conn, monthly_credit_budget=400, cost=2, now="2026-08-09T04:00:00Z"))
        self.assertTrue(within_budget(self.conn, monthly_credit_budget=400, cost=1, now="2026-08-09T04:00:00Z"))

    def test_budget_resets_on_a_new_calendar_period_without_needing_a_successful_call(self):
        # The whole point of tracking by period rather than mirroring the
        # API's own counter: exhausting budget in August must not leave the
        # feed permanently refusing calls once September starts.
        record_call_cost(self.conn, 400, now="2026-08-09T00:00:00Z")
        self.assertFalse(within_budget(self.conn, monthly_credit_budget=400, cost=1, now="2026-08-31T23:00:00Z"))
        self.assertTrue(within_budget(self.conn, monthly_credit_budget=400, cost=1, now="2026-09-01T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
