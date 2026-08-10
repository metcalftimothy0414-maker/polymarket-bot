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
TRADE_HISTORY_PER_MARKET = 50

# Hardcoded risk rule (not configurable): order-book data older than this is
# treated as unusable, per the "never trade on stale data" requirement.
STALE_SECONDS = 30


def league_of(market: dict) -> str | None:
    """The market "category" field is always "sports" (confirmed against live
    data — it never carries mlb/nba/ufc/etc.), so it can't filter by league.
    The slug's 2nd hyphen-segment is the only field present on every market
    (team-based and individual sports alike) that reliably encodes it, e.g.
    "tec-mlb-nlchamp-2026-09-27-nym" -> "mlb", "aec-ufc-padpim-bensai-..." -> "ufc"."""
    parts = market.get("slug", "").split("-")
    return parts[1] if len(parts) >= 2 else None


def filter_by_leagues(markets: list[dict], leagues: list[str]) -> list[dict]:
    if not leagues:
        return markets
    wanted = {league.lower() for league in leagues}
    return [m for m in markets if (league_of(m) or "").lower() in wanted]


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

    async def discover_all_markets(self, closed: bool = False, page_limit: int = 500) -> list[dict]:
        """Every market across every category in one paginated scan, no
        `categories` filter — confirmed live that /v1/markets returns all
        of sports/politics/culture/finance/geopolitics/technology/macro/
        crypto/science/climate from a single unfiltered call, so this is
        one scan rather than discover_markets()'s one-scan-per-category
        (which would need the category list known and kept up to date)."""
        results: list[dict] = []
        offset = 0
        while True:
            await self._bucket.acquire()
            resp = await self._client.get(
                "/v1/markets",
                params={"limit": page_limit, "offset": offset, "closed": str(closed).lower()},
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


class PolymarketRestPoller:
    """Fallback for MarketState when the authenticated WS stream isn't usable
    (e.g. a network that strips/mangles the custom X-PM-* auth headers) —
    polls the public REST book endpoint instead. Same STALE_SECONDS rule and
    duck-typed .books/.is_stale(...) surface as PolymarketWSClient, so
    MarketState doesn't care which one is driving it.
    """

    def __init__(self, rest_client: PolymarketRestClient, poll_seconds: float = 3.0) -> None:
        self.rest_client = rest_client
        self.poll_seconds = poll_seconds
        self.books: dict[str, dict] = {}
        # There's no public REST trades endpoint, only WS (SUBSCRIPTION_TYPE_TRADE) —
        # always empty here.
        self.trades: dict[str, list[dict]] = {}
        self._last_update: dict[str, float] = {}

    def is_stale(self, slug: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        ts = self._last_update.get(slug)
        return ts is None or (now - ts) > STALE_SECONDS

    async def poll(
        self, market_slugs: list[str], stop_event: asyncio.Event | None = None,
        on_success: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            for slug in market_slugs:
                try:
                    book = await self.rest_client.get_book(slug)
                    self.books[slug] = book
                    self._last_update[slug] = time.monotonic()
                    if on_success:
                        on_success()
                except httpx.HTTPError as exc:
                    logger.warning("REST book poll failed for %s: %s", slug, exc)
                    if on_error:
                        on_error(str(exc))
            await asyncio.sleep(self.poll_seconds)


class PolymarketWSClient:
    """Streams order-book updates for a watchlist of market slugs.

    Auto-reconnects with exponential backoff; every disconnect is logged.
    """

    def __init__(self, auth: PolymarketAuth, ws_url: str = WS_URL) -> None:
        self.auth = auth
        self.ws_url = ws_url
        self.books: dict[str, dict] = {}
        # Bounded rolling window per market — enough for Strategy D's rolling-median
        # baseline without unbounded memory growth on a long-running connection.
        self.trades: dict[str, list[dict]] = {}
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
        on_success: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        if len(market_slugs) > MAX_MARKETS_PER_CONNECTION:
            raise ValueError(f"max {MAX_MARKETS_PER_CONNECTION} markets per WS connection, got {len(market_slugs)}")

        backoff = 1.0
        while stop_event is None or not stop_event.is_set():
            try:
                headers = self.auth.headers("GET", WS_PATH)
                async with websockets.connect(self.ws_url, additional_headers=headers) as ws:
                    backoff = 1.0
                    if on_success:
                        on_success()  # a live connection is itself a success signal
                    await ws.send(json.dumps({
                        "subscribe": {
                            "requestId": "md-sub-1",
                            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                            "marketSlugs": market_slugs,
                        }
                    }))
                    await ws.send(json.dumps({
                        "subscribe": {
                            "requestId": "trade-sub-1",
                            "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
                            "marketSlugs": market_slugs,
                        }
                    }))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if (data := msg.get("marketData")) is not None:
                            slug = data["marketSlug"]
                            self.books[slug] = data
                            self._last_update[slug] = time.monotonic()
                            await on_update(data)
                        elif (trade := msg.get("trade")) is not None:
                            slug = trade["marketSlug"]
                            history = self.trades.setdefault(slug, [])
                            history.append(trade)
                            del history[:-TRADE_HISTORY_PER_MARKET]
            except (websockets.exceptions.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                logger.warning("Polymarket WS disconnected (%s); reconnecting in %.0fs", exc, backoff)
                if on_error:
                    on_error(str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
