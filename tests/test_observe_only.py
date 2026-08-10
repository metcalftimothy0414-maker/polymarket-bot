from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from bot.db import SCHEMA
from bot.matching.cli import pairs_review

OBSERVE_ONLY = {"economic_indicator", "politics_elections", "numeric_threshold", "generic"}


def _insert_pair(conn, pair_id, category):
    conn.execute(
        "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, category, created_at) "
        "VALUES (?, ?, ?, 1.0, 0, ?, 'now')",
        (pair_id, f"slug-{pair_id}", f"K-{pair_id}", category),
    )
    conn.commit()


class ObserveOnlyEnforcementTests(unittest.TestCase):
    """§9 acceptance criterion: no pair in an observe-only category can
    reach verified=TRUE, regardless of what a reviewer types — the check
    happens where the UPDATE actually runs, not just in the CLI prompt."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_typing_yes_on_an_observe_only_pair_does_not_verify_it(self):
        _insert_pair(self.conn, 1, "economic_indicator")
        with patch("builtins.input", side_effect=["y", "s"]):
            pairs_review(self.conn, observe_only_categories=OBSERVE_ONLY)
        verified = self.conn.execute("SELECT verified FROM pairs WHERE id = 1").fetchone()[0]
        self.assertEqual(verified, 0)

    def test_sports_pair_can_still_be_verified_normally(self):
        _insert_pair(self.conn, 1, "sports")
        with patch("builtins.input", side_effect=["y", "s"]):
            pairs_review(self.conn, observe_only_categories=OBSERVE_ONLY)
        verified = self.conn.execute("SELECT verified FROM pairs WHERE id = 1").fetchone()[0]
        self.assertEqual(verified, 1)

    def test_every_observe_only_category_is_blocked(self):
        for i, category in enumerate(OBSERVE_ONLY, start=1):
            with self.subTest(category=category):
                conn = sqlite3.connect(":memory:")
                conn.executescript(SCHEMA)
                _insert_pair(conn, i, category)
                with patch("builtins.input", side_effect=["y", "s"]):
                    pairs_review(conn, observe_only_categories=OBSERVE_ONLY)
                verified = conn.execute("SELECT verified FROM pairs WHERE id = ?", (i,)).fetchone()[0]
                self.assertEqual(verified, 0, f"{category} pair was verified — observe-only gate failed")

    def test_observe_only_pair_can_still_be_rejected(self):
        _insert_pair(self.conn, 1, "economic_indicator")
        with patch("builtins.input", side_effect=["n"]):
            pairs_review(self.conn, observe_only_categories=OBSERVE_ONLY)
        count = self.conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        self.assertEqual(count, 0)  # rejected and deleted, not silently kept

    def test_no_observe_only_config_means_no_restriction(self):
        # Calling pairs_review without observe_only_categories at all
        # (e.g. an older call site) must not accidentally block everything.
        _insert_pair(self.conn, 1, "economic_indicator")
        with patch("builtins.input", side_effect=["y", "s"]):
            pairs_review(self.conn, observe_only_categories=None)
        verified = self.conn.execute("SELECT verified FROM pairs WHERE id = 1").fetchone()[0]
        self.assertEqual(verified, 1)


if __name__ == "__main__":
    unittest.main()
