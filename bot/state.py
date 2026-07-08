from __future__ import annotations

import time
from typing import Protocol

KALSHI_STALE_SECONDS = 30
ODDS_API_STALE_SECONDS = 60  # odds move slower than order books; polled, not streamed


class _VenueCache:
    """Generic last-value + staleness tracker for a polled (non-WS) venue."""

    def __init__(self, stale_seconds: float) -> None:
        self.stale_seconds = stale_seconds
        self._data: dict[str, dict] = {}
        self._last_seen: dict[str, float] = {}

    def update(self, key: str, value: dict) -> None:
        self._data[key] = value
        self._last_seen[key] = time.monotonic()

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def is_stale(self, key: str, max_age: float | None = None) -> bool:
        ts = self._last_seen.get(key)
        return ts is None or (time.monotonic() - ts) > (max_age if max_age is not None else self.stale_seconds)


class PolymarketBookSource(Protocol):
    """Either PolymarketWSClient (streaming) or PolymarketRestPoller (fallback
    for a network that blocks the authenticated WS handshake) satisfies this."""

    books: dict[str, dict]
    trades: dict[str, list[dict]]

    def is_stale(self, slug: str, now: float | None = None) -> bool: ...


class MarketState:
    """Shared read surface strategies poll — feeds are the only writers."""

    def __init__(self, polymarket_ws: PolymarketBookSource) -> None:
        self.polymarket_ws = polymarket_ws
        self.kalshi = _VenueCache(KALSHI_STALE_SECONDS)
        self.odds_api = _VenueCache(ODDS_API_STALE_SECONDS)

    def polymarket_book(self, slug: str) -> dict | None:
        return self.polymarket_ws.books.get(slug)

    def polymarket_is_stale(self, slug: str) -> bool:
        return self.polymarket_ws.is_stale(slug)

    def polymarket_trades(self, slug: str) -> list[dict]:
        return self.polymarket_ws.trades.get(slug, [])
