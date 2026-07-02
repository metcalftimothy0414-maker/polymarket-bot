from __future__ import annotations

import time

from bot.feeds.polymarket import PolymarketWSClient

KALSHI_STALE_SECONDS = 30


class MarketState:
    """Shared read surface strategies poll — feeds are the only writers."""

    def __init__(self, polymarket_ws: PolymarketWSClient) -> None:
        self.polymarket_ws = polymarket_ws
        self.kalshi_books: dict[str, dict] = {}
        self._kalshi_last_seen: dict[str, float] = {}

    def polymarket_book(self, slug: str) -> dict | None:
        return self.polymarket_ws.books.get(slug)

    def polymarket_is_stale(self, slug: str) -> bool:
        return self.polymarket_ws.is_stale(slug)

    def update_kalshi(self, ticker: str, book: dict) -> None:
        self.kalshi_books[ticker] = book
        self._kalshi_last_seen[ticker] = time.monotonic()

    def kalshi_book(self, ticker: str) -> dict | None:
        return self.kalshi_books.get(ticker)

    def kalshi_is_stale(self, ticker: str, max_age: float = KALSHI_STALE_SECONDS) -> bool:
        ts = self._kalshi_last_seen.get(ticker)
        return ts is None or (time.monotonic() - ts) > max_age
