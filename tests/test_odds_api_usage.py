from __future__ import annotations

import sqlite3
import unittest

import httpx

from bot.db import SCHEMA
from bot.odds_api_usage import latest_usage, record_usage


class OddsApiUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_records_credits_from_headers(self):
        headers = httpx.Headers({"x-requests-remaining": "480", "x-requests-used": "20"})
        record_usage(self.conn, headers, "/v4/sports/baseball_mlb/odds/", now="2026-08-09T00:00:00Z")
        usage = latest_usage(self.conn)
        self.assertEqual(usage["credits_remaining"], 480)
        self.assertEqual(usage["credits_used"], 20)

    def test_records_even_on_an_exhausted_quota_error_response(self):
        # Confirmed live: the 401 OUT_OF_USAGE_CREDITS response still carries these headers.
        headers = httpx.Headers({"x-requests-remaining": "0", "x-requests-used": "500"})
        record_usage(self.conn, headers, "/v4/sports/baseball_mlb/odds/")
        usage = latest_usage(self.conn)
        self.assertEqual(usage["credits_remaining"], 0)

    def test_no_usage_yet_returns_none(self):
        self.assertIsNone(latest_usage(self.conn))

    def test_non_odds_api_response_headers_are_not_recorded(self):
        headers = httpx.Headers({"content-type": "application/json"})
        record_usage(self.conn, headers, "/some/other/endpoint")
        self.assertIsNone(latest_usage(self.conn))

    def test_latest_usage_returns_the_most_recent_row(self):
        record_usage(self.conn, httpx.Headers({"x-requests-remaining": "480", "x-requests-used": "20"}), "e", now="2026-08-09T00:00:00Z")
        record_usage(self.conn, httpx.Headers({"x-requests-remaining": "479", "x-requests-used": "21"}), "e", now="2026-08-09T01:00:00Z")
        usage = latest_usage(self.conn)
        self.assertEqual(usage["credits_remaining"], 479)


if __name__ == "__main__":
    unittest.main()
