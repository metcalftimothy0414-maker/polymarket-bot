from __future__ import annotations

import sqlite3
import unittest

from bot.db import SCHEMA
from bot.feed_health import all_feed_statuses, feed_status, record_error, record_success


class FeedHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_never_seen_feed_is_idle_not_degraded(self):
        status = feed_status(self.conn, "odds_api")
        self.assertEqual(status["status"], "IDLE")
        self.assertIsNone(status["last_success_at"])

    def test_success_only_is_idle(self):
        record_success(self.conn, "kalshi", now="2026-08-09T00:00:00Z")
        status = feed_status(self.conn, "kalshi")
        self.assertEqual(status["status"], "IDLE")
        self.assertEqual(status["last_success_at"], "2026-08-09T00:00:00Z")

    def test_error_only_is_degraded(self):
        record_error(self.conn, "odds_api", "401 OUT_OF_USAGE_CREDITS", now="2026-08-09T00:00:00Z")
        status = feed_status(self.conn, "odds_api")
        self.assertEqual(status["status"], "DEGRADED")
        self.assertEqual(status["last_error_message"], "401 OUT_OF_USAGE_CREDITS")

    def test_error_after_success_is_degraded(self):
        record_success(self.conn, "odds_api", now="2026-08-09T00:00:00Z")
        record_error(self.conn, "odds_api", "quota exhausted", now="2026-08-09T01:00:00Z")
        status = feed_status(self.conn, "odds_api")
        self.assertEqual(status["status"], "DEGRADED")

    def test_success_after_error_recovers_to_idle(self):
        record_error(self.conn, "odds_api", "quota exhausted", now="2026-08-09T00:00:00Z")
        record_success(self.conn, "odds_api", now="2026-08-09T01:00:00Z")
        status = feed_status(self.conn, "odds_api")
        self.assertEqual(status["status"], "IDLE")
        self.assertIsNone(status["last_error_message"])  # cleared on recovery, not just shadowed

    def test_message_truncated_to_500_chars(self):
        record_error(self.conn, "odds_api", "x" * 1000)
        status = feed_status(self.conn, "odds_api")
        self.assertEqual(len(status["last_error_message"]), 500)

    def test_all_feed_statuses_covers_every_known_feed(self):
        statuses = all_feed_statuses(self.conn)
        self.assertEqual(set(statuses.keys()), {"polymarket", "kalshi", "odds_api"})

    def test_feeds_track_independently(self):
        record_error(self.conn, "odds_api", "boom", now="2026-08-09T00:00:00Z")
        record_success(self.conn, "kalshi", now="2026-08-09T00:00:00Z")
        statuses = all_feed_statuses(self.conn)
        self.assertEqual(statuses["odds_api"]["status"], "DEGRADED")
        self.assertEqual(statuses["kalshi"]["status"], "IDLE")


if __name__ == "__main__":
    unittest.main()
