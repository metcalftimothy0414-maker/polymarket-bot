from __future__ import annotations

import csv
import os
import random
import sqlite3
import unittest

from bot.db import SCHEMA
from bot.report import base_rate_test, evaluate_kill_criteria, export_csv, max_drawdown, strategy_metrics


def insert_trade(conn, strategy_id="divergence", status="closed", realized_pnl_usd=None,
                  fill_price=0.50, notional_usd=10.0, exit_reason=None, closed_at=None):
    conn.execute(
        "INSERT INTO paper_trades (strategy_id, params_hash, market_ref, direction, signal_entry_price, "
        "fill_price, notional_usd, entry_fee, exit_reason, realized_pnl_usd, status, opened_at, closed_at) "
        "VALUES (?, 'h', 'slug', 'buy_polymarket', ?, ?, ?, 0, ?, ?, ?, 'now', ?)",
        (strategy_id, fill_price, fill_price, notional_usd, exit_reason, realized_pnl_usd, status, closed_at),
    )


def insert_opportunity(conn, strategy_id="divergence", persistence_seconds=None):
    conn.execute(
        "INSERT INTO opportunities (strategy_id, params_hash, market_ref, direction, signal_value, "
        "entry_price, detected_at, last_seen_at, status, persistence_seconds) "
        "VALUES (?, 'h', 'slug', 'buy_polymarket', 0.05, 0.5, 'now', 'now', 'closed', ?)",
        (strategy_id, persistence_seconds),
    )


class TestMaxDrawdown(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_tracks_worst_peak_to_trough(self):
        for i, pnl in enumerate([10, 5, -20, 3]):
            insert_trade(self.conn, realized_pnl_usd=pnl, closed_at=f"2026-01-01T00:0{i}:00")
        self.conn.commit()
        # cumulative: 10, 15, -5, -2 | peak: 10, 15, 15, 15 | drawdown: 0, 0, 20, 17
        self.assertEqual(max_drawdown(self.conn, "divergence"), 20)

    def test_zero_when_no_trades(self):
        self.assertEqual(max_drawdown(self.conn, "divergence"), 0.0)


class TestStrategyMetrics(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_basic_metrics(self):
        insert_trade(self.conn, status="unfilled", realized_pnl_usd=None)
        insert_trade(self.conn, status="closed", realized_pnl_usd=5.0, fill_price=0.50, notional_usd=10)
        insert_trade(self.conn, status="closed", realized_pnl_usd=-2.0, fill_price=0.50, notional_usd=10)
        insert_opportunity(self.conn, persistence_seconds=15)
        insert_opportunity(self.conn, persistence_seconds=25)
        self.conn.commit()

        m = strategy_metrics(self.conn, "divergence")
        self.assertEqual(m["opportunities_detected"], 2)
        self.assertEqual(m["median_persistence_seconds"], 20)
        self.assertAlmostEqual(m["fill_rate"], 2 / 3)
        self.assertAlmostEqual(m["win_rate"], 1 / 2)
        self.assertEqual(m["total_pnl_usd"], 3.0)


class TestKillCriteria(unittest.TestCase):
    def test_insufficient_data_is_not_dead(self):
        metrics = {"opportunities_detected": 5, "fill_rate": 0.1, "avg_edge_cents": 0.1, "median_persistence_seconds": 1}
        is_dead, reasons = evaluate_kill_criteria(metrics)
        self.assertFalse(is_dead)
        self.assertIn("insufficient data", reasons[0])

    def test_dead_on_low_fill_rate(self):
        metrics = {"opportunities_detected": 150, "fill_rate": 0.15, "avg_edge_cents": 5.0, "median_persistence_seconds": 30}
        is_dead, reasons = evaluate_kill_criteria(metrics)
        self.assertTrue(is_dead)
        self.assertTrue(any("fill rate" in r for r in reasons))

    def test_dead_on_low_edge(self):
        metrics = {"opportunities_detected": 150, "fill_rate": 0.50, "avg_edge_cents": 0.3, "median_persistence_seconds": 30}
        is_dead, reasons = evaluate_kill_criteria(metrics)
        self.assertTrue(is_dead)
        self.assertTrue(any("avg edge" in r for r in reasons))

    def test_dead_on_low_persistence(self):
        metrics = {"opportunities_detected": 150, "fill_rate": 0.50, "avg_edge_cents": 5.0, "median_persistence_seconds": 3}
        is_dead, reasons = evaluate_kill_criteria(metrics)
        self.assertTrue(is_dead)
        self.assertTrue(any("persistence" in r for r in reasons))

    def test_alive_when_all_thresholds_met(self):
        metrics = {"opportunities_detected": 150, "fill_rate": 0.50, "avg_edge_cents": 5.0, "median_persistence_seconds": 30}
        is_dead, reasons = evaluate_kill_criteria(metrics)
        self.assertFalse(is_dead)
        self.assertEqual(reasons, [])


class TestBaseRateTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_insufficient_below_200_resolved(self):
        for _ in range(50):
            insert_trade(self.conn, strategy_id="sports_momentum", exit_reason="resolution",
                         fill_price=0.55, realized_pnl_usd=1.0)
        self.conn.commit()
        result = base_rate_test(self.conn, "sports_momentum")
        self.assertFalse(result["sufficient"])

    def test_zero_edge_matches_entry_price_not_distinguishable(self):
        random.seed(42)
        for _ in range(400):
            won = random.random() < 0.55  # win rate matches the 0.55 entry price -> zero edge
            pnl = 0.45 if won else -0.55
            insert_trade(self.conn, strategy_id="sports_momentum", exit_reason="resolution",
                         fill_price=0.55, realized_pnl_usd=pnl)
        self.conn.commit()
        result = base_rate_test(self.conn, "sports_momentum")
        self.assertTrue(result["sufficient"])
        self.assertFalse(result["distinguishable"])

    def test_real_edge_is_distinguishable(self):
        for _ in range(400):
            insert_trade(self.conn, strategy_id="sports_momentum", exit_reason="resolution",
                         fill_price=0.55, realized_pnl_usd=1.0)  # every trade wins, way above 55% base rate
        self.conn.commit()
        result = base_rate_test(self.conn, "sports_momentum")
        self.assertTrue(result["sufficient"])
        self.assertTrue(result["distinguishable"])
        self.assertTrue(result["outperforming"])


class TestExportCsv(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.out_path = "data/test_export.csv"

    def tearDown(self):
        if os.path.exists(self.out_path):
            os.remove(self.out_path)

    def test_exports_all_trades(self):
        insert_trade(self.conn, realized_pnl_usd=5.0)
        insert_trade(self.conn, realized_pnl_usd=-2.0)
        self.conn.commit()
        count = export_csv(self.conn, self.out_path)
        self.assertEqual(count, 2)
        with open(self.out_path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)

    def test_empty_returns_zero(self):
        count = export_csv(self.conn, self.out_path)
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
