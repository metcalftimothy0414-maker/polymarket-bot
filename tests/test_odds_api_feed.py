from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from bot.feeds.odds_api import OddsApiFeedClient


def _resp(status_code: int, json_body, headers: dict) -> MagicMock:
    resp = MagicMock()
    resp.headers = httpx.Headers(headers)
    resp.json = MagicMock(return_value=json_body)
    if status_code >= 400:
        resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock(status_code=status_code),
        ))
    else:
        resp.raise_for_status = MagicMock()
    return resp


class OddsApiFeedClientTests(unittest.TestCase):
    def test_markets_and_regions_accept_lists_and_join_with_comma(self):
        client = OddsApiFeedClient("key", "https://example.invalid", regions=["us", "uk"], markets=["h2h"])
        self.assertEqual(client.regions, "us,uk")
        self.assertEqual(client.markets, "h2h")

    def test_get_odds_calls_on_headers_even_when_it_raises(self):
        async def scenario():
            client = OddsApiFeedClient("key", "https://example.invalid")
            resp = _resp(401, {"error_code": "OUT_OF_USAGE_CREDITS"}, {"x-requests-remaining": "0", "x-requests-used": "500"})
            captured = []
            with patch.object(client._client, "get", AsyncMock(return_value=resp)):
                with self.assertRaises(httpx.HTTPStatusError):
                    await client.get_odds("baseball_mlb", on_headers=lambda h: captured.append(dict(h)))
            await client.aclose()
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["x-requests-remaining"], "0")

        asyncio.run(scenario())

    def test_poll_calls_on_success_and_on_error(self):
        async def scenario():
            client = OddsApiFeedClient("key", "https://example.invalid")
            client.get_odds = AsyncMock(return_value=[{"id": "g1"}])

            successes = []
            stop_event = asyncio.Event()
            task = asyncio.create_task(client.poll(
                ["baseball_mlb"], lambda k, g: asyncio.sleep(0), poll_seconds=0.01, stop_event=stop_event,
                on_success=lambda: successes.append(1),
            ))
            await asyncio.sleep(0.03)
            stop_event.set()
            await task
            await client.aclose()
            self.assertGreater(len(successes), 0)

        asyncio.run(scenario())

    def test_poll_reports_error_on_http_failure(self):
        async def scenario():
            client = OddsApiFeedClient("key", "https://example.invalid")
            client.get_odds = AsyncMock(side_effect=httpx.HTTPStatusError(
                "quota", request=MagicMock(), response=MagicMock(status_code=401),
            ))

            errors = []
            stop_event = asyncio.Event()
            task = asyncio.create_task(client.poll(
                ["baseball_mlb"], lambda k, g: asyncio.sleep(0), poll_seconds=0.01, stop_event=stop_event,
                on_error=lambda msg: errors.append(msg),
            ))
            await asyncio.sleep(0.03)
            stop_event.set()
            await task
            await client.aclose()
            self.assertGreater(len(errors), 0)

        asyncio.run(scenario())

    def test_should_call_false_skips_the_request_entirely(self):
        async def scenario():
            client = OddsApiFeedClient("key", "https://example.invalid")
            client.get_odds = AsyncMock(return_value=[{"id": "g1"}])

            stop_event = asyncio.Event()
            task = asyncio.create_task(client.poll(
                ["baseball_mlb"], lambda k, g: asyncio.sleep(0), poll_seconds=0.01, stop_event=stop_event,
                should_call=lambda: False,
            ))
            await asyncio.sleep(0.03)
            stop_event.set()
            await task
            await client.aclose()
            client.get_odds.assert_not_called()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
