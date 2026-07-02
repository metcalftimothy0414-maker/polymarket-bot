from __future__ import annotations

import asyncio
import unittest

from bot.feeds.polymarket import PolymarketRestPoller


class FakeRestClient:
    def __init__(self, books: dict) -> None:
        self._books = books
        self.calls = []

    async def get_book(self, slug: str) -> dict:
        self.calls.append(slug)
        return self._books[slug]


class TestPolymarketRestPoller(unittest.TestCase):
    def test_is_stale_before_any_poll(self):
        poller = PolymarketRestPoller(FakeRestClient({}))
        self.assertTrue(poller.is_stale("slug"))

    def test_poll_populates_books_and_marks_fresh(self):
        rest = FakeRestClient({"slug-a": {"bids": [], "offers": []}})
        poller = PolymarketRestPoller(rest, poll_seconds=0.01)

        async def scenario():
            stop_event = asyncio.Event()
            task = asyncio.create_task(poller.poll(["slug-a"], stop_event))
            await asyncio.sleep(0.03)
            stop_event.set()
            await task

        asyncio.run(scenario())
        self.assertIn("slug-a", poller.books)
        self.assertFalse(poller.is_stale("slug-a"))
        self.assertGreaterEqual(len(rest.calls), 1)

    def test_failed_fetch_does_not_crash_poll_loop(self):
        class FlakyRestClient:
            async def get_book(self, slug):
                raise __import__("httpx").HTTPError("boom")

        poller = PolymarketRestPoller(FlakyRestClient(), poll_seconds=0.01)

        async def scenario():
            stop_event = asyncio.Event()
            task = asyncio.create_task(poller.poll(["slug-a"], stop_event))
            await asyncio.sleep(0.03)
            stop_event.set()
            await task

        asyncio.run(scenario())  # must not raise
        self.assertTrue(poller.is_stale("slug-a"))


if __name__ == "__main__":
    unittest.main()
