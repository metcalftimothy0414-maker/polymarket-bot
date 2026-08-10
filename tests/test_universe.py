from __future__ import annotations

import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.db import SCHEMA
from bot.feeds.kalshi import KalshiFeedClient
from bot.feeds.polymarket import PolymarketRestClient
from bot.universe import catalog_refresh_loop, discover_kalshi_catalog, discover_polymarket_catalog, kalshi_series_ticker


class KalshiSeriesTickerTests(unittest.TestCase):
    def test_strips_everything_after_first_hyphen(self):
        self.assertEqual(kalshi_series_ticker("KXMLBGAME-26AUG121610MILSD-SD"), "KXMLBGAME")
        self.assertEqual(kalshi_series_ticker("KXMLBGAME-26AUG121610MILSD"), "KXMLBGAME")
        self.assertEqual(kalshi_series_ticker("KXBTCY"), "KXBTCY")


class DiscoverPolymarketCatalogTests(unittest.TestCase):
    def test_writes_markets_and_preserves_first_seen(self):
        async def scenario():
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            client = PolymarketRestClient(rate_limit_per_sec=1000)
            client.discover_all_markets = AsyncMock(return_value=[
                {"slug": "a-slug", "question": "Q1", "category": "politics", "marketType": "moneyline",
                 "description": "d1", "orderPriceMinTickSize": 0.01, "negRisk": False,
                 "gameStartTime": None, "endDate": "2026-12-31T00:00:00Z", "closed": False},
            ])
            n = await discover_polymarket_catalog(client, conn)
            self.assertEqual(n, 1)
            row = conn.execute("SELECT category, market_type, first_seen, last_seen FROM polymarket_catalog WHERE slug='a-slug'").fetchone()
            self.assertEqual(row[0], "politics")
            self.assertEqual(row[1], "moneyline")
            first_seen_1 = row[2]

            # Re-run: last_seen updates, first_seen does not.
            await discover_polymarket_catalog(client, conn)
            row2 = conn.execute("SELECT first_seen, last_seen FROM polymarket_catalog WHERE slug='a-slug'").fetchone()
            self.assertEqual(row2[0], first_seen_1)

            await client.aclose()

        asyncio.run(scenario())

    def test_skips_markets_without_a_slug(self):
        async def scenario():
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            client = PolymarketRestClient(rate_limit_per_sec=1000)
            client.discover_all_markets = AsyncMock(return_value=[{"question": "no slug here"}])
            n = await discover_polymarket_catalog(client, conn)
            self.assertEqual(n, 0)
            await client.aclose()

        asyncio.run(scenario())


class DiscoverKalshiCatalogTests(unittest.TestCase):
    def test_writes_markets_with_category_joined_from_series(self):
        async def scenario():
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            client = KalshiFeedClient("https://example.invalid", poll_seconds=1)
            client.get_series_list = AsyncMock(return_value=[{"ticker": "KXMLBGAME", "category": "Sports"}])
            client.get_markets_bulk = AsyncMock(return_value=[
                {
                    "ticker": "KXMLBGAME-26AUG121610MILSD-SD", "event_ticker": "KXMLBGAME-26AUG121610MILSD",
                    "title": "Milwaukee vs San Diego", "rules_primary": "r1", "rules_secondary": "r2",
                    "close_time": "2026-08-13T00:00:00Z", "expiration_time": "2026-08-13T00:00:00Z",
                    "occurrence_datetime": "2026-08-12T20:10:00Z", "status": "open",
                },
            ])
            n = await discover_kalshi_catalog(client, conn)
            self.assertEqual(n, 1)
            row = conn.execute(
                "SELECT series_ticker, category FROM kalshi_catalog WHERE ticker='KXMLBGAME-26AUG121610MILSD-SD'"
            ).fetchone()
            self.assertEqual(row, ("KXMLBGAME", "Sports"))
            await client.aclose()

        asyncio.run(scenario())

    def test_unknown_series_gets_null_category_not_a_crash(self):
        async def scenario():
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            client = KalshiFeedClient("https://example.invalid", poll_seconds=1)
            client.get_series_list = AsyncMock(return_value=[])
            client.get_markets_bulk = AsyncMock(return_value=[
                {"ticker": "KXNEW-1-A", "event_ticker": "KXNEW-1", "title": "t"},
            ])
            n = await discover_kalshi_catalog(client, conn)
            self.assertEqual(n, 1)
            row = conn.execute("SELECT category FROM kalshi_catalog WHERE ticker='KXNEW-1-A'").fetchone()
            self.assertIsNone(row[0])
            await client.aclose()

        asyncio.run(scenario())


class GetMarketsBulkTests(unittest.TestCase):
    def test_paginates_and_paces(self):
        async def scenario():
            client = KalshiFeedClient("https://example.invalid", poll_seconds=1)
            pages = [
                MagicMock(json=MagicMock(return_value={"markets": [{"ticker": "A"}], "cursor": "next"})),
                MagicMock(json=MagicMock(return_value={"markets": [{"ticker": "B"}], "cursor": ""})),
            ]
            for p in pages:
                p.raise_for_status = MagicMock()
            with patch.object(client._client, "get", AsyncMock(side_effect=pages)):
                markets = await client.get_markets_bulk(pace_seconds=0)
            await client.aclose()
            self.assertEqual([m["ticker"] for m in markets], ["A", "B"])

        asyncio.run(scenario())


class DiscoverAllMarketsTests(unittest.TestCase):
    def test_paginates_without_category_param(self):
        async def scenario():
            client = PolymarketRestClient(rate_limit_per_sec=1000)
            pages = [
                MagicMock(json=MagicMock(return_value={"markets": [{"slug": "a"}] * 500})),
                MagicMock(json=MagicMock(return_value={"markets": [{"slug": "b"}]})),
            ]
            for p in pages:
                p.raise_for_status = MagicMock()
            with patch.object(client._client, "get", AsyncMock(side_effect=pages)) as mock_get:
                markets = await client.discover_all_markets(closed=False, page_limit=500)
            await client.aclose()
            self.assertEqual(len(markets), 501)
            # confirm no `categories` param was sent
            for call in mock_get.call_args_list:
                self.assertNotIn("categories", call.kwargs.get("params", {}))

        asyncio.run(scenario())


class CatalogRefreshLoopTests(unittest.TestCase):
    def test_one_iteration_refreshes_polling_tiers(self):
        async def scenario():
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO pairs (id, polymarket_slug, kalshi_ticker, similarity_score, verified, tier, created_at) "
                "VALUES (1, 's', 'k', 1.0, 1, NULL, 'now')"
            )
            conn.commit()

            kalshi_client = KalshiFeedClient("https://example.invalid", poll_seconds=1)
            kalshi_client.get_series_list = AsyncMock(return_value=[])
            kalshi_client.get_markets_bulk = AsyncMock(return_value=[])
            rest_client = PolymarketRestClient(rate_limit_per_sec=1000)
            rest_client.discover_all_markets = AsyncMock(return_value=[])

            stop_event = asyncio.Event()

            async def stop_after_one_pass():
                await asyncio.sleep(0.05)
                stop_event.set()

            await asyncio.gather(
                catalog_refresh_loop(kalshi_client, rest_client, conn, stop_event, interval_seconds=0.01),
                stop_after_one_pass(),
            )
            await kalshi_client.aclose()
            await rest_client.aclose()

            row = conn.execute("SELECT polling_tier FROM pairs WHERE id = 1").fetchone()
            self.assertEqual(row[0], "A")  # verified=1 -> Tier A, proves refresh_polling_tiers ran

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
