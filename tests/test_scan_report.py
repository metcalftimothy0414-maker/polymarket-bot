from __future__ import annotations

import sqlite3
import unittest

from bot.db import SCHEMA
from bot.scan_report import catalog_summary, category_report


def _insert_pair(conn, pair_id, category, tier):
    conn.execute(
        "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, category, tier, created_at) "
        "VALUES (?, ?, ?, 1.0, 0, ?, ?, 'now')",
        (pair_id, f"slug-{pair_id}", f"K-{pair_id}", category, tier),
    )


def _insert_eval(conn, pair_id, *, traded, binding_constraint, net_edge=None, annual_return=None):
    conn.execute(
        "INSERT INTO pair_evaluations (pair_id, ts, direction, net_edge_per_contract, annualized_return, "
        "traded, binding_constraint) VALUES (?, 'now', 'd', ?, ?, ?, ?)",
        (pair_id, net_edge, annual_return, int(traded), binding_constraint),
    )


class CatalogSummaryTests(unittest.TestCase):
    def test_counts_by_category(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO kalshi_catalog VALUES ('t1','s1',NULL,'x','Sports',NULL,NULL,NULL,NULL,NULL,'open','now','now')")
        conn.execute("INSERT INTO kalshi_catalog VALUES ('t2','s2',NULL,'x','Sports',NULL,NULL,NULL,NULL,NULL,'open','now','now')")
        conn.execute("INSERT INTO kalshi_catalog VALUES ('t3','s3',NULL,'x','Politics',NULL,NULL,NULL,NULL,NULL,'open','now','now')")
        conn.execute("INSERT INTO polymarket_catalog VALUES ('slug1','q','sports','moneyline',NULL,NULL,0,NULL,NULL,'open','now','now')")
        conn.commit()
        summary = catalog_summary(conn)
        self.assertEqual(summary["kalshi_total"], 3)
        self.assertEqual(summary["kalshi_by_category"]["Sports"], 2)
        self.assertEqual(summary["polymarket_total"], 1)


class CategoryReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_empty_category_returns_zeros_not_crash(self):
        r = category_report(self.conn, "economic_indicator")
        self.assertEqual(r["pairs_matched"], 0)
        self.assertEqual(r["opportunities_detected"], 0)
        self.assertIsNone(r["median_net_edge"])
        self.assertIsNone(r["most_common_binding_constraint"])

    def test_counts_pairs_and_tiers_for_the_category_only(self):
        _insert_pair(self.conn, 1, "sports", 1)
        _insert_pair(self.conn, 2, "sports", 2)
        _insert_pair(self.conn, 3, "economic_indicator", 1)
        self.conn.commit()
        r = category_report(self.conn, "sports")
        self.assertEqual(r["pairs_matched"], 2)
        self.assertEqual(r["pairs_by_tier"], {1: 1, 2: 1})

    def test_opportunities_and_gate_breakdown(self):
        _insert_pair(self.conn, 1, "sports", 1)
        _insert_eval(self.conn, 1, traded=True, binding_constraint="passed", net_edge=0.02, annual_return=0.5)
        _insert_eval(self.conn, 1, traded=False, binding_constraint="below_min_edge")
        _insert_eval(self.conn, 1, traded=False, binding_constraint="below_min_edge")
        self.conn.commit()
        r = category_report(self.conn, "sports")
        self.assertEqual(r["opportunities_detected"], 3)
        self.assertEqual(r["gate_counts"]["below_min_edge"], 2)
        self.assertEqual(r["most_common_binding_constraint"], "below_min_edge")
        self.assertEqual(r["median_net_edge"], 0.02)
        self.assertEqual(r["median_annual_return"], 0.5)

    def test_does_not_leak_across_categories(self):
        _insert_pair(self.conn, 1, "sports", 1)
        _insert_pair(self.conn, 2, "economic_indicator", 1)
        _insert_eval(self.conn, 1, traded=True, binding_constraint="passed", net_edge=0.10)
        _insert_eval(self.conn, 2, traded=True, binding_constraint="passed", net_edge=0.99)
        self.conn.commit()
        sports_report = category_report(self.conn, "sports")
        self.assertEqual(sports_report["median_net_edge"], 0.10)

    def test_persistence_stats_from_divergence_periods(self):
        self.conn.execute(
            "INSERT INTO divergence_periods (pair_id, category, tier, opened_at, closed_at, peak_edge, duration_seconds) "
            "VALUES (1, 'sports', 1, 'now', 'later', 0.05, 100)"
        )
        self.conn.execute(
            "INSERT INTO divergence_periods (pair_id, category, tier, opened_at, closed_at, peak_edge, duration_seconds) "
            "VALUES (2, 'sports', 1, 'now', 'later', 0.03, 300)"
        )
        self.conn.commit()
        r = category_report(self.conn, "sports")
        self.assertEqual(r["median_persistence_seconds"], 200)
        self.assertEqual(r["persistence_samples"], 2)


if __name__ == "__main__":
    unittest.main()
