from __future__ import annotations

import sqlite3
import unittest

from bot.db import SCHEMA
from bot.export_dashboard import build_snapshot


class BuildSnapshotTests(unittest.TestCase):
    def test_empty_db_does_not_raise_and_has_expected_shape(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        snapshot = build_snapshot(conn)

        self.assertEqual(snapshot["mode"], "PAPER")
        self.assertIn("generated_at", snapshot)
        self.assertIn("kalshi_divergence", snapshot["strategies"])
        self.assertIn("sportsbook_divergence", snapshot["strategies"])
        self.assertEqual(snapshot["pair_counts"]["kalshi_pairs_total"], 0)
        self.assertEqual(snapshot["recent_opportunities"], [])
        self.assertFalse(snapshot["feed_health"]["runner_alive"])

    def test_heartbeat_marks_runner_alive(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        conn.execute("INSERT INTO heartbeats (ts, component, detail) VALUES (?, 'runner', 'alive')", (now,))
        conn.commit()
        snapshot = build_snapshot(conn)
        self.assertTrue(snapshot["feed_health"]["runner_alive"])

    def test_feed_statuses_present_for_all_known_feeds(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        snapshot = build_snapshot(conn)
        for name in ("polymarket", "kalshi", "odds_api"):
            self.assertIn(name, snapshot["feed_statuses"])
            self.assertEqual(snapshot["feed_statuses"][name]["status"], "IDLE")

    def test_odds_api_credits_reflects_recorded_usage(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO odds_api_usage (ts, credits_remaining, credits_used, endpoint) VALUES (?, ?, ?, ?)",
            ("2026-08-09T00:00:00Z", 10, 90, "/v4/sports/odds"),
        )
        conn.commit()
        snapshot = build_snapshot(conn)
        self.assertEqual(snapshot["odds_api_credits"]["credits_remaining"], 10)
        self.assertTrue(snapshot["odds_api_credits"]["low_credits_alert"])  # 10% remaining <= 20% alert threshold

    def test_old_errors_are_excluded_from_recent_errors(self):
        import datetime as dt
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=38)).isoformat()
        fresh = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
        conn.execute("INSERT INTO errors (ts, component, message) VALUES (?, 'scan_loop', 'ancient')", (old,))
        conn.execute("INSERT INTO errors (ts, component, message) VALUES (?, 'scan_loop', 'fresh')", (fresh,))
        conn.commit()
        snapshot = build_snapshot(conn)
        messages = [e["message"] for e in snapshot["recent_errors"]]
        self.assertIn("fresh", messages)
        self.assertNotIn("ancient", messages)


if __name__ == "__main__":
    unittest.main()
