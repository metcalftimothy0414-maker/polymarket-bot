from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock

from bot import edge
from bot.db import SCHEMA
from bot.fee_multipliers import (
    ZERO_FEE_SERIES,
    _seconds_since_last_refresh,
    load_into_edge_module,
    refresh_kalshi_multipliers,
)


class RefreshKalshiMultipliersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_sports_series_get_1_1(self):
        client = AsyncMock()
        client.get_series_list = AsyncMock(return_value=[{"ticker": "KXNBAGAME"}, {"ticker": "KXMLBGAME"}])
        asyncio.run(refresh_kalshi_multipliers(client, self.conn))
        row = self.conn.execute(
            "SELECT m_taker, m_maker, source FROM kalshi_series_multipliers WHERE series_ticker = 'KXNBAGAME'"
        ).fetchone()
        self.assertEqual(row, (1.0, 1.0, "category_sports"))

    def test_zero_fee_series_get_0_0(self):
        client = AsyncMock()
        client.get_series_list = AsyncMock(return_value=[])
        asyncio.run(refresh_kalshi_multipliers(client, self.conn))
        for ticker in ZERO_FEE_SERIES:
            row = self.conn.execute(
                "SELECT m_taker, m_maker, source FROM kalshi_series_multipliers WHERE series_ticker = ?", (ticker,)
            ).fetchone()
            self.assertEqual(row, (0.0, 0.0, "known_zero_fee_list"))

    def test_rerun_updates_fetched_at_not_duplicates(self):
        client = AsyncMock()
        client.get_series_list = AsyncMock(return_value=[{"ticker": "KXNBAGAME"}])
        asyncio.run(refresh_kalshi_multipliers(client, self.conn))
        asyncio.run(refresh_kalshi_multipliers(client, self.conn))
        count = self.conn.execute(
            "SELECT COUNT(*) FROM kalshi_series_multipliers WHERE series_ticker = 'KXNBAGAME'"
        ).fetchone()[0]
        self.assertEqual(count, 1)


class LoadIntoEdgeModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        edge.KALSHI_SERIES_MULTIPLIERS.clear()

    def tearDown(self) -> None:
        edge.KALSHI_SERIES_MULTIPLIERS.clear()

    def test_loads_rows_into_edge_dict(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO kalshi_series_multipliers VALUES ('KXNBAGAME', 1, 1, 'category_sports', ?)", (now,)
        )
        self.conn.commit()
        n = load_into_edge_module(self.conn)
        self.assertEqual(n, 1)
        self.assertEqual(edge.KALSHI_SERIES_MULTIPLIERS["KXNBAGAME"], (Decimal(1), Decimal(1)))

    def test_zero_zero_series_produces_zero_fee_end_to_end(self):
        # Acceptance criterion: fee engine returns zero for a 0/0 series.
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO kalshi_series_multipliers VALUES ('KXBTCY', 0, 0, 'known_zero_fee_list', ?)", (now,)
        )
        self.conn.commit()
        load_into_edge_module(self.conn)
        fee = edge.kalshi_fee(Decimal("0.50"), 100, maker=False, series="KXBTCY")
        self.assertEqual(fee, Decimal(0))
        maker_fee = edge.kalshi_fee(Decimal("0.50"), 100, maker=True, series="KXBTCY")
        self.assertEqual(maker_fee, Decimal(0))

    def test_unlisted_series_has_zero_maker_fee(self):
        # Acceptance criterion: zero maker fee for an unlisted series (raw
        # published default M_maker=0, not a rescued value).
        load_into_edge_module(self.conn)  # empty table
        maker_fee = edge.kalshi_fee(Decimal("0.50"), 100, maker=True, series="KXSOMETHINGNEW")
        self.assertEqual(maker_fee, Decimal(0))
        taker_fee = edge.kalshi_fee(Decimal("0.50"), 100, maker=False, series="KXSOMETHINGNEW")
        self.assertEqual(taker_fee, Decimal("1.7500"))  # M_taker default 1


class StalenessTests(unittest.TestCase):
    def test_no_rows_is_stale(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        self.assertIsNone(_seconds_since_last_refresh(conn))

    def test_recent_fetch_is_not_stale(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        conn.execute("INSERT INTO kalshi_series_multipliers VALUES ('X', 1, 0, 'default', ?)", (now,))
        conn.commit()
        age = _seconds_since_last_refresh(conn)
        self.assertLess(age, 5)


if __name__ == "__main__":
    unittest.main()
