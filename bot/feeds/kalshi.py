from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

# NOTE: unlike feeds/polymarket.py, this client has not been exercised against
# the live API from this dev machine — api.elections.kalshi.com is behind a
# local TLS-intercepting proxy (NetAlerts) on this network, untrusted, so all
# calls to it are blocked here by the data_collection_enabled config gate.
# Smoke-test this against the real API on the VPS before relying on it.


class KalshiFeedClient:
    """Read-only, unauthenticated polling client for Kalshi's public market data."""

    def __init__(self, base_url: str, poll_seconds: int, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self.poll_seconds = poll_seconds

    async def get_orderbook(self, ticker: str) -> dict:
        resp = await self._client.get(f"/markets/{ticker}/orderbook")
        resp.raise_for_status()
        return resp.json()["orderbook"]

    async def poll(
        self,
        tickers: list[str],
        on_update: Callable[[str, dict], Awaitable[None]],
        stop_event: asyncio.Event | None = None,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            for ticker in tickers:
                try:
                    book = await self.get_orderbook(ticker)
                    await on_update(ticker, book)
                except httpx.HTTPError as exc:
                    logger.warning("Kalshi poll failed for %s: %s", ticker, exc)
            await asyncio.sleep(self.poll_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()
