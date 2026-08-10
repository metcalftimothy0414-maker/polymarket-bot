from __future__ import annotations

import sqlite3
import unittest

from bot.db import SCHEMA
from bot.persistence_tracking import update_divergence_period


class UpdateDivergencePeriodTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_positive_edge_opens_a_new_period(self):
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.02, now="2026-08-09T00:00:00Z")
        row = tuple(self.conn.execute("SELECT category, tier, opened_at, peak_edge, closed_at FROM divergence_periods").fetchone())
        self.assertEqual(row, ("sports", 1, "2026-08-09T00:00:00Z", 0.02, None))

    def test_no_open_period_and_no_edge_does_nothing(self):
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=None, now="2026-08-09T00:00:00Z")
        count = self.conn.execute("SELECT COUNT(*) FROM divergence_periods").fetchone()[0]
        self.assertEqual(count, 0)

    def test_repeated_positive_edge_tracks_the_peak(self):
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.02, now="2026-08-09T00:00:00Z")
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.05, now="2026-08-09T00:01:00Z")
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.01, now="2026-08-09T00:02:00Z")
        row = tuple(self.conn.execute("SELECT peak_edge, closed_at FROM divergence_periods").fetchone())
        self.assertEqual(row, (0.05, None))  # peak stays at the max seen, period still open

    def test_edge_dropping_to_zero_closes_the_period(self):
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.02, now="2026-08-09T00:00:00Z")
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.0, now="2026-08-09T00:05:00Z")
        row = tuple(self.conn.execute("SELECT closed_at, duration_seconds FROM divergence_periods").fetchone())
        self.assertEqual(row, ("2026-08-09T00:05:00Z", 300.0))

    def test_missing_evaluation_closes_the_period_too(self):
        # e.g. a stale_book skip has gross_edge=None — the divergence isn't
        # observably present anymore even if we don't know its current value.
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.02, now="2026-08-09T00:00:00Z")
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=None, now="2026-08-09T00:01:00Z")
        row = tuple(self.conn.execute("SELECT closed_at FROM divergence_periods").fetchone())
        self.assertIsNotNone(row[0])

    def test_new_period_opens_after_a_previous_one_closed(self):
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.02, now="2026-08-09T00:00:00Z")
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.0, now="2026-08-09T00:01:00Z")
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.03, now="2026-08-09T00:02:00Z")
        count = self.conn.execute("SELECT COUNT(*) FROM divergence_periods").fetchone()[0]
        self.assertEqual(count, 2)
        open_count = self.conn.execute("SELECT COUNT(*) FROM divergence_periods WHERE closed_at IS NULL").fetchone()[0]
        self.assertEqual(open_count, 1)

    def test_different_pairs_track_independently(self):
        update_divergence_period(self.conn, pair_id=1, category="sports", tier=1, gross_edge=0.02, now="2026-08-09T00:00:00Z")
        update_divergence_period(self.conn, pair_id=2, category="economic_indicator", tier=2, gross_edge=0.10, now="2026-08-09T00:00:00Z")
        rows = [tuple(r) for r in self.conn.execute("SELECT pair_id, category FROM divergence_periods ORDER BY pair_id").fetchall()]
        self.assertEqual(rows, [(1, "sports"), (2, "economic_indicator")])


if __name__ == "__main__":
    unittest.main()
