from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.feeds.kalshi import KalshiFeedClient, KalshiResponseError


class TestKalshiFeedClient(unittest.TestCase):
    def test_get_orderbook_raises_clear_error_on_missing_key(self):
        async def scenario():
            client = KalshiFeedClient("https://example.invalid", poll_seconds=1)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"error": "not found"})  # no "orderbook" key
            with patch.object(client._client, "get", AsyncMock(return_value=resp)):
                with self.assertRaises(KalshiResponseError):
                    await client.get_orderbook("BAD-TICKER")
            await client.aclose()

        asyncio.run(scenario())

    def test_poll_skips_bad_ticker_instead_of_crashing(self):
        """A single malformed response must not kill the whole poll loop —
        every other ticker (and every other strategy relying on this feed)
        depends on this loop staying alive."""
        async def scenario():
            client = KalshiFeedClient("https://example.invalid", poll_seconds=0.01)
            updates = []

            async def fake_get_orderbook(ticker):
                if ticker == "BAD":
                    raise KalshiResponseError("boom")
                return {"yes": [[50, 100]]}

            client.get_orderbook = fake_get_orderbook

            async def on_update(ticker, book):
                updates.append(ticker)

            stop_event = asyncio.Event()
            task = asyncio.create_task(client.poll(["BAD", "GOOD"], on_update, stop_event))
            await asyncio.sleep(0.05)
            stop_event.set()
            await task
            await client.aclose()

            self.assertIn("GOOD", updates)
            self.assertNotIn("BAD", updates)

        asyncio.run(scenario())

    def test_get_series_list_follows_pagination_cursor(self):
        async def scenario():
            client = KalshiFeedClient("https://example.invalid", poll_seconds=1)
            pages = [
                MagicMock(json=MagicMock(return_value={"series": [{"ticker": "A"}], "cursor": "next"})),
                MagicMock(json=MagicMock(return_value={"series": [{"ticker": "B"}], "cursor": ""})),
            ]
            for p in pages:
                p.raise_for_status = MagicMock()
            with patch.object(client._client, "get", AsyncMock(side_effect=pages)):
                series = await client.get_series_list(category="Sports")
            await client.aclose()
            self.assertEqual([s["ticker"] for s in series], ["A", "B"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
