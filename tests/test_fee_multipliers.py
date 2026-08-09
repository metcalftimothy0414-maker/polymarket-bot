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
    _seconds_since_last_refresh,
    load_into_edge_module,
    refresh_kalshi_multipliers,
)


def _series(ticker: str, fee_multiplier, fee_type) -> dict:
    return {"ticker": ticker, "fee_multiplier": fee_multiplier, "fee_type": fee_type}


class RefreshKalshiMultipliersTests(unittest.TestCase):
    """fee_multiplier/fee_type come straight off Kalshi's live /series
    response — confirmed live 2026-08-09 that fee_multiplier IS M_taker and
    M_maker equals it only when fee_type ends "_with_maker_fees" (e.g.
    KXMLBGAME is 0.5/0.5, not the flat 1/1 sports was assumed to carry;
    KXNFLPASSYDS is Sports but plain "quadratic" -> 1/0; KXBTCY etc are 0/0)."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_maker_fee_type_series_gets_matching_m_maker(self):
        client = AsyncMock()
        client.get_series_list = AsyncMock(return_value=[_series("KXMLBGAME", 0.5, "quadratic_with_maker_fees")])
        asyncio.run(refresh_kalshi_multipliers(client, self.conn))
        row = self.conn.execute(
            "SELECT m_taker, m_maker, source FROM kalshi_series_multipliers WHERE series_ticker = 'KXMLBGAME'"
        ).fetchone()
        self.assertEqual(row, (0.5, 0.5, "kalshi_series_api"))

    def test_plain_quadratic_sports_series_has_zero_maker_fee(self):
        # Sports category, but no maker fees — the "all sports = 1/1"
        # assumption was wrong; the live fee_type field is authoritative.
        client = AsyncMock()
        client.get_series_list = AsyncMock(return_value=[_series("KXNFLPASSYDS", 1, "quadratic")])
        asyncio.run(refresh_kalshi_multipliers(client, self.conn))
        row = self.conn.execute(
            "SELECT m_taker, m_maker FROM kalshi_series_multipliers WHERE series_ticker = 'KXNFLPASSYDS'"
        ).fetchone()
        self.assertEqual(row, (1.0, 0.0))

    def test_zero_fee_series_get_0_0(self):
        client = AsyncMock()
        client.get_series_list = AsyncMock(return_value=[
            _series(t, 0, "quadratic") for t in
            ("KXBTCY", "KXETHY", "KXGREENLAND", "KXDOED", "KXLAYOFFSYINFO",
             "KXCITRINI", "KXELECTIRAN", "KXIRANDEMOCRACY", "KXPAHLAVIHEAD", "KXGAMBLINGREPEAL")
        ])
        asyncio.run(refresh_kalshi_multipliers(client, self.conn))
        for ticker in ("KXBTCY", "KXETHY", "KXGREENLAND", "KXDOED", "KXLAYOFFSYINFO",
                       "KXCITRINI", "KXELECTIRAN", "KXIRANDEMOCRACY", "KXPAHLAVIHEAD", "KXGAMBLINGREPEAL"):
            row = self.conn.execute(
                "SELECT m_taker, m_maker FROM kalshi_series_multipliers WHERE series_ticker = ?", (ticker,)
            ).fetchone()
            self.assertEqual(row, (0.0, 0.0), f"{ticker} should be 0/0")

    def test_series_with_no_fee_data_is_skipped_not_defaulted(self):
        client = AsyncMock()
        client.get_series_list = AsyncMock(return_value=[_series("KXLEGACY", None, None)])
        written = asyncio.run(refresh_kalshi_multipliers(client, self.conn))
        self.assertEqual(written, 0)
        row = self.conn.execute(
            "SELECT * FROM kalshi_series_multipliers WHERE series_ticker = 'KXLEGACY'"
        ).fetchone()
        self.assertIsNone(row)

    def test_rerun_updates_fetched_at_not_duplicates(self):
        client = AsyncMock()
        client.get_series_list = AsyncMock(return_value=[_series("KXNBAGAME", 1, "quadratic_with_maker_fees")])
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
            "INSERT INTO kalshi_series_multipliers VALUES ('KXNBAGAME', 1, 1, 'kalshi_series_api', ?)", (now,)
        )
        self.conn.commit()
        n = load_into_edge_module(self.conn)
        self.assertEqual(n, 1)
        self.assertEqual(edge.KALSHI_SERIES_MULTIPLIERS["KXNBAGAME"], (Decimal(1), Decimal(1)))

    def test_zero_zero_series_produces_zero_fee_end_to_end(self):
        # Acceptance criterion: fee engine returns zero for a 0/0 series.
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO kalshi_series_multipliers VALUES ('KXBTCY', 0, 0, 'kalshi_series_api', ?)", (now,)
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
