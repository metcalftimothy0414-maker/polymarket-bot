from __future__ import annotations

import sqlite3
import unittest

from bot.db import SCHEMA
from bot.polling_tiers import (
    RateBudget,
    classify_pair_tier,
    demote_inactive_tier_b_pairs,
    poll_interval_seconds,
    refresh_polling_tiers,
)


class RateBudgetTests(unittest.TestCase):
    def test_acquires_up_to_the_cap(self):
        budget = RateBudget(max_requests_per_minute=3)
        self.assertTrue(budget.try_acquire(now=0))
        self.assertTrue(budget.try_acquire(now=0))
        self.assertTrue(budget.try_acquire(now=0))
        self.assertFalse(budget.try_acquire(now=0))  # 4th in the same instant fails

    def test_old_requests_age_out_of_the_window(self):
        budget = RateBudget(max_requests_per_minute=1)
        self.assertTrue(budget.try_acquire(now=0))
        self.assertFalse(budget.try_acquire(now=30))  # still within 60s window
        self.assertTrue(budget.try_acquire(now=61))  # first request has aged out

    def test_utilization_reflects_current_load(self):
        budget = RateBudget(max_requests_per_minute=10)
        for _ in range(5):
            budget.try_acquire(now=0)
        self.assertAlmostEqual(budget.utilization(now=0), 0.5)

    def test_never_exceeds_cap_under_repeated_bursts(self):
        budget = RateBudget(max_requests_per_minute=5)
        acquired = sum(1 for _ in range(20) if budget.try_acquire(now=0))
        self.assertEqual(acquired, 5)


class ClassifyPairTierTests(unittest.TestCase):
    def test_verified_is_always_tier_a(self):
        self.assertEqual(classify_pair_tier(verified=True, tier=5, max_reviewable_tier=3), "A")
        self.assertEqual(classify_pair_tier(verified=True, tier=None, max_reviewable_tier=3), "A")

    def test_unverified_within_reviewable_tier_is_b(self):
        self.assertEqual(classify_pair_tier(verified=False, tier=1, max_reviewable_tier=3), "B")
        self.assertEqual(classify_pair_tier(verified=False, tier=3, max_reviewable_tier=3), "B")

    def test_unverified_above_reviewable_tier_is_c(self):
        self.assertEqual(classify_pair_tier(verified=False, tier=4, max_reviewable_tier=3), "C")
        self.assertEqual(classify_pair_tier(verified=False, tier=5, max_reviewable_tier=3), "C")

    def test_null_tier_defaults_to_reviewable(self):
        self.assertEqual(classify_pair_tier(verified=False, tier=None, max_reviewable_tier=3), "B")


class PollIntervalTests(unittest.TestCase):
    def test_returns_the_right_interval_per_tier(self):
        kwargs = {"tier_a_seconds": 15, "tier_b_seconds": 60, "tier_c_seconds": 600}
        self.assertEqual(poll_interval_seconds("A", **kwargs), 15)
        self.assertEqual(poll_interval_seconds("B", **kwargs), 60)
        self.assertEqual(poll_interval_seconds("C", **kwargs), 600)


class RefreshPollingTiersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def _insert_pair(self, pair_id: int, verified: int, tier: int | None) -> None:
        self.conn.execute(
            "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, tier, created_at) "
            "VALUES (?, ?, ?, 1.0, ?, ?, 'now')",
            (pair_id, f"slug-{pair_id}", f"K-{pair_id}", verified, tier),
        )
        self.conn.commit()

    def test_assigns_tiers_to_all_pairs(self):
        self._insert_pair(1, verified=1, tier=None)
        self._insert_pair(2, verified=0, tier=2)
        self._insert_pair(3, verified=0, tier=5)
        counts = refresh_polling_tiers(self.conn, max_reviewable_tier=3)
        self.assertEqual(counts, {"A": 1, "B": 1, "C": 1})
        rows = dict(self.conn.execute("SELECT id, polling_tier FROM pairs").fetchall())
        self.assertEqual(rows, {1: "A", 2: "B", 3: "C"})


class DemoteInactiveTierBPairsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, tier, "
            "polling_tier, created_at) VALUES (1, 's', 'k', 1.0, 0, 2, 'B', 'now')"
        )
        self.conn.commit()

    def _log_evals(self, n: int, gross_edge) -> None:
        for i in range(n):
            self.conn.execute(
                "INSERT INTO pair_evaluations (pair_id, ts, direction, gross_edge_per_contract, traded, "
                "binding_constraint) VALUES (1, ?, 'd', ?, 0, 'x')",
                (f"2026-08-{i+1:02d}T00:00:00Z", gross_edge),
            )
        self.conn.commit()

    def test_demotes_a_pair_with_no_positive_edge_in_recent_history(self):
        self._log_evals(15, gross_edge=-0.05)
        demoted = demote_inactive_tier_b_pairs(self.conn)
        self.assertEqual(demoted, 1)
        row = self.conn.execute("SELECT polling_tier FROM pairs WHERE id = 1").fetchone()
        self.assertEqual(row[0], "C")

    def test_does_not_demote_a_pair_with_some_positive_edge(self):
        self._log_evals(10, gross_edge=-0.05)
        self.conn.execute(
            "INSERT INTO pair_evaluations (pair_id, ts, direction, gross_edge_per_contract, traded, binding_constraint) "
            "VALUES (1, '2026-08-20T00:00:00Z', 'd', 0.02, 0, 'x')"
        )
        self.conn.commit()
        demoted = demote_inactive_tier_b_pairs(self.conn)
        self.assertEqual(demoted, 0)
        row = self.conn.execute("SELECT polling_tier FROM pairs WHERE id = 1").fetchone()
        self.assertEqual(row[0], "B")

    def test_does_not_demote_a_pair_with_too_little_history(self):
        self._log_evals(5, gross_edge=-0.05)  # below DEMOTION_MIN_EVALUATIONS
        demoted = demote_inactive_tier_b_pairs(self.conn)
        self.assertEqual(demoted, 0)

    def test_only_considers_tier_b_pairs(self):
        self.conn.execute("UPDATE pairs SET polling_tier = 'C' WHERE id = 1")
        self.conn.commit()
        self._log_evals(15, gross_edge=-0.05)
        demoted = demote_inactive_tier_b_pairs(self.conn)
        self.assertEqual(demoted, 0)


class RateBudgetGatesACatalogScanTests(unittest.TestCase):
    """§6 acceptance criterion: a full catalog scan completes without
    exceeding the configured rate limit, proven by a test that asserts on
    request counts. Simulates the real scale confirmed live 2026-08-09
    (766,612 Kalshi markets => ~767 pages at limit=1000) against a budget
    tight enough that, without gating, the burst would blow through it."""

    def test_paginated_scan_never_exceeds_the_per_minute_cap(self):
        total_pages = 767
        budget = RateBudget(max_requests_per_minute=20)
        granted = 0
        denied_then_retried = 0
        t = 0.0
        page = 0
        while page < total_pages:
            if budget.try_acquire(now=t):
                granted += 1
                page += 1
            else:
                denied_then_retried += 1
                t += 3.0  # back off and retry, simulating a real polling loop's sleep
                continue
            self.assertLessEqual(budget.current_rate(now=t), 20)

        self.assertEqual(page, total_pages)
        self.assertGreater(denied_then_retried, 0)  # the cap actually bound at some point

    def test_utilization_metric_would_trigger_an_alert_before_the_cap_binds(self):
        budget = RateBudget(max_requests_per_minute=100)
        for _ in range(85):
            budget.try_acquire(now=0)
        # "alert if it approaches the cap" (§6) — 85% utilization is the
        # kind of value a metric/alert threshold would fire on.
        self.assertGreater(budget.utilization(now=0), 0.8)


if __name__ == "__main__":
    unittest.main()
