from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


class KalshiResponseError(Exception):
    """A 2xx response that doesn't have the shape we expect (e.g. an unknown/
    closed ticker) — distinct from httpx.HTTPError so poll() can treat both as
    a per-ticker skip-and-continue rather than crashing the whole runner."""

# NOTE: api.elections.kalshi.com is behind a local TLS-intercepting proxy
# (NetAlerts) on this network and was unreachable for most of this project —
# confirmed reachable over a VPN. Still exercise this against the real API
# again before relying on it for anything beyond a manual trial.


class KalshiFeedClient:
    """Read-only, unauthenticated polling client for Kalshi's public market data."""

    def __init__(self, base_url: str, poll_seconds: int, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self.poll_seconds = poll_seconds

    async def get_orderbook(self, ticker: str) -> dict:
        resp = await self._client.get(f"/markets/{ticker}/orderbook")
        resp.raise_for_status()
        body = resp.json()
        if "orderbook" not in body:
            raise KalshiResponseError(f"no 'orderbook' key in response for {ticker}: {body}")
        return body["orderbook"]

    async def get_market(self, ticker: str) -> dict:
        resp = await self._client.get(f"/markets/{ticker}")
        resp.raise_for_status()
        body = resp.json()
        if "market" not in body:
            raise KalshiResponseError(f"no 'market' key in response for {ticker}: {body}")
        return body["market"]

    async def get_markets(self, series_ticker: str, status: str = "open", limit: int = 200) -> list[dict]:
        markets: list[dict] = []
        cursor = None
        while True:
            params = {"series_ticker": series_ticker, "status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            resp = await self._client.get("/markets", params=params)
            resp.raise_for_status()
            body = resp.json()
            markets.extend(body.get("markets", []))
            cursor = body.get("cursor")
            if not cursor:
                break
        return markets

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
                except (httpx.HTTPError, KalshiResponseError) as exc:
                    logger.warning("Kalshi poll failed for %s: %s", ticker, exc)
            await asyncio.sleep(self.poll_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()
