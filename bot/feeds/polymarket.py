from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import httpx
import websockets

from bot.feeds.auth import PolymarketAuth
from bot.rate_limiter import TokenBucket

logger = logging.getLogger(__name__)

REST_BASE_URL = "https://gateway.polymarket.us"
WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_PATH = "/v1/ws/markets"
MAX_MARKETS_PER_CONNECTION = 100

# Hardcoded risk rule (not configurable): order-book data older than this is
# treated as unusable, per the "never trade on stale data" requirement.
STALE_SECONDS = 30


class PolymarketRestClient:
    def __init__(self, rate_limit_per_sec: float, base_url: str = REST_BASE_URL, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._bucket = TokenBucket(rate_limit_per_sec)

    async def discover_markets(self, categories: list[str], closed: bool = False, page_limit: int = 100) -> list[dict]:
        results: list[dict] = []
        for category in categories:
            offset = 0
            while True:
                await self._bucket.acquire()
                resp = await self._client.get(
                    "/v1/markets",
                    params={"categories": category, "limit": page_limit, "offset": offset, "closed": str(closed).lower()},
                )
                resp.raise_for_status()
                page = resp.json().get("markets", [])
                results.extend(page)
                if len(page) < page_limit:
                    break
                offset += page_limit
        return results

    async def get_book(self, slug: str) -> dict:
        await self._bucket.acquire()
        resp = await self._client.get(f"/v1/markets/{slug}/book")
        resp.raise_for_status()
        return resp.json()["marketData"]

    async def aclose(self) -> None:
        await self._client.aclose()


class PolymarketWSClient:
    """Streams order-book updates for a watchlist of market slugs.

    Auto-reconnects with exponential backoff; every disconnect is logged.
    """

    def __init__(self, auth: PolymarketAuth, ws_url: str = WS_URL) -> None:
        self.auth = auth
        self.ws_url = ws_url
        self.books: dict[str, dict] = {}
        self._last_update: dict[str, float] = {}

    def is_stale(self, slug: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        ts = self._last_update.get(slug)
        return ts is None or (now - ts) > STALE_SECONDS

    async def stream(
        self,
        market_slugs: list[str],
        on_update: Callable[[dict], Awaitable[None]],
        stop_event: asyncio.Event | None = None,
    ) -> None:
        if len(market_slugs) > MAX_MARKETS_PER_CONNECTION:
            raise ValueError(f"max {MAX_MARKETS_PER_CONNECTION} markets per WS connection, got {len(market_slugs)}")

        backoff = 1.0
        while stop_event is None or not stop_event.is_set():
            try:
                headers = self.auth.headers("GET", WS_PATH)
                async with websockets.connect(self.ws_url, additional_headers=headers) as ws:
                    backoff = 1.0
                    await ws.send(json.dumps({
                        "subscribe": {
                            "requestId": "md-sub-1",
                            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                            "marketSlugs": market_slugs,
                        }
                    }))
                    async for raw in ws:
                        data = json.loads(raw).get("marketData")
                        if not data:
                            continue
                        slug = data["marketSlug"]
                        self.books[slug] = data
                        self._last_update[slug] = time.monotonic()
                        await on_update(data)
            except (websockets.exceptions.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                logger.warning("Polymarket WS disconnected (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
