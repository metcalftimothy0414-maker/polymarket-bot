from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from bot.db import SCHEMA
from bot.matching.cli import pairs_review


def _insert_pair(conn, pair_id, category, tier, similarity=0.8):
    conn.execute(
        "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, category, tier, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, 'now')",
        (pair_id, f"slug-{pair_id}", f"K-{pair_id}", similarity, category, tier),
    )


def _insert_eval(conn, pair_id, annual_return, ts="2026-08-09T00:00:00Z"):
    conn.execute(
        "INSERT INTO pair_evaluations (pair_id, ts, direction, annualized_return, traded, binding_constraint) "
        "VALUES (?, ?, 'd', ?, 0, 'below_annual_hurdle')",
        (pair_id, ts, annual_return),
    )


class PairsReviewFiltersTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def _captured_rows(self, **kwargs) -> list:
        with patch("bot.matching.cli._interactive_review") as mock_review:
            pairs_review(self.conn, **kwargs)
        return mock_review.call_args[0][2]  # (conn, table, rows, print_row)

    def test_no_filters_returns_all_unverified(self):
        _insert_pair(self.conn, 1, "sports", 1)
        _insert_pair(self.conn, 2, "economic_indicator", 2)
        self.conn.commit()
        rows = self._captured_rows()
        self.assertEqual(len(rows), 2)

    def test_category_filter(self):
        _insert_pair(self.conn, 1, "sports", 1)
        _insert_pair(self.conn, 2, "economic_indicator", 2)
        self.conn.commit()
        rows = self._captured_rows(category="sports")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 1)

    def test_max_tier_filter(self):
        _insert_pair(self.conn, 1, "sports", 1)
        _insert_pair(self.conn, 2, "sports", 5)
        self.conn.commit()
        rows = self._captured_rows(max_tier=3)
        self.assertEqual([r[0] for r in rows], [1])

    def test_null_tier_survives_max_tier_filter(self):
        # A pair with no tier assigned yet shouldn't be silently excluded
        # by a filter that post-dates it.
        _insert_pair(self.conn, 1, "sports", None)
        self.conn.commit()
        rows = self._captured_rows(max_tier=3)
        self.assertEqual(len(rows), 1)

    def test_verified_pairs_never_appear(self):
        _insert_pair(self.conn, 1, "sports", 1)
        self.conn.execute("UPDATE pairs SET verified = 1 WHERE id = 1")
        self.conn.commit()
        rows = self._captured_rows()
        self.assertEqual(rows, [])

    def test_sorted_by_latest_annualized_return_descending(self):
        _insert_pair(self.conn, 1, "sports", 1, similarity=0.9)
        _insert_pair(self.conn, 2, "sports", 1, similarity=0.9)
        _insert_pair(self.conn, 3, "sports", 1, similarity=0.9)
        _insert_eval(self.conn, 1, annual_return=0.10)
        _insert_eval(self.conn, 2, annual_return=0.80)
        _insert_eval(self.conn, 3, annual_return=0.30)
        self.conn.commit()
        rows = self._captured_rows()
        self.assertEqual([r[0] for r in rows], [2, 3, 1])

    def test_most_recent_evaluation_wins_not_the_first(self):
        _insert_pair(self.conn, 1, "sports", 1)
        _insert_eval(self.conn, 1, annual_return=0.05, ts="2026-08-01T00:00:00Z")
        _insert_eval(self.conn, 1, annual_return=0.99, ts="2026-08-09T00:00:00Z")
        self.conn.commit()
        rows = self._captured_rows()
        self.assertAlmostEqual(rows[0][-1], 0.99)

    def test_pairs_with_no_evaluation_sort_last(self):
        _insert_pair(self.conn, 1, "sports", 1)  # no evaluation logged
        _insert_pair(self.conn, 2, "sports", 1)
        _insert_eval(self.conn, 2, annual_return=0.01)
        self.conn.commit()
        rows = self._captured_rows()
        self.assertEqual([r[0] for r in rows], [2, 1])


if __name__ == "__main__":
    unittest.main()
