from __future__ import annotations

import asyncio
import base64
import os
import unittest
from unittest.mock import AsyncMock, patch

from bot.config import (
    FeedsConfig,
    KalshiDivergenceStrategyConfig,
    KalshiFeedConfig,
    LockedPairArbStrategyConfig,
    LogConfig,
    OddsApiFeedConfig,
    PolymarketFeedConfig,
    Settings,
    SportsbookDivergenceStrategyConfig,
    StrategiesConfig,
)
from bot.runner import run


def _settings(tmp_db_path: str) -> Settings:
    return Settings(
        data_collection_enabled=True,
        feeds=FeedsConfig(
            polymarket=PolymarketFeedConfig(categories=["sports"], watchlist_slugs=[]),
            kalshi=KalshiFeedConfig(enabled=True),
            odds_api=OddsApiFeedConfig(enabled=True),
        ),
        strategies=StrategiesConfig(
            kalshi_divergence=KalshiDivergenceStrategyConfig(),
            sportsbook_divergence=SportsbookDivergenceStrategyConfig(),
            locked_pair_arb=LockedPairArbStrategyConfig(),
        ),
        log=LogConfig(file=tmp_db_path + ".log"),
        polymarket_api_key_id="dummy",
        polymarket_private_key=base64.b64encode(os.urandom(32)).decode(),
        odds_api_key="dummy",
    )


class FakeWSStream:
    """Stands in for PolymarketWSClient.stream: just waits for shutdown."""

    def __init__(self) -> None:
        self.last_market_slugs: list[str] | None = None

    async def __call__(self, market_slugs, on_update, stop_event=None):
        self.last_market_slugs = market_slugs
        if stop_event is not None:
            await stop_event.wait()


class TestRunnerWiring(unittest.TestCase):
    """One end-to-end smoke test: the runner is glue code that's never been
    executed against a real network from this environment (see task notes on
    WS-auth being blocked). This proves the wiring itself — imports, method
    signatures, argument order across feeds/strategies/paper — doesn't crash,
    without needing real network access.
    """

    def test_one_scan_iteration_does_not_crash(self):
        async def scenario():
            db_path = "data/test_runner_smoke.db"
            import bot.db as db_module
            original_connect = db_module.connect
            db_module.connect = lambda: original_connect(db_path)
            try:
                settings = _settings(db_path)
                with patch("bot.runner.PolymarketRestClient") as MockRest, \
                     patch("bot.runner.PolymarketWSClient") as MockWS, \
                     patch("bot.runner.KalshiFeedClient") as MockKalshi:
                    mock_rest = MockRest.return_value
                    mock_rest.discover_markets = AsyncMock(return_value=[])
                    mock_rest.discover_all_markets = AsyncMock(return_value=[])
                    mock_rest.aclose = AsyncMock()
                    mock_ws = MockWS.return_value
                    mock_ws.stream = FakeWSStream()
                    mock_kalshi = MockKalshi.return_value
                    mock_kalshi.get_series_list = AsyncMock(return_value=[])
                    mock_kalshi.get_markets_bulk = AsyncMock(return_value=[])

                    try:
                        await asyncio.wait_for(run(settings), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass  # expected — background loops run forever until stopped
            finally:
                db_module.connect = original_connect
                if os.path.exists(db_path):
                    os.remove(db_path)

        asyncio.run(scenario())

    def test_verified_pair_slug_streamed_even_if_outside_discovery_autofill(self):
        """A pair can be verified without its Polymarket slug ever landing in the
        naive first-N discovery auto-fill (or an explicit watchlist) — the runner
        must union it in regardless, or the strategy that owns it never sees a book."""
        async def scenario():
            db_path = "data/test_runner_smoke2.db"
            import bot.db as db_module
            original_connect = db_module.connect
            db_module.connect = lambda: original_connect(db_path)
            try:
                conn = original_connect(db_path)
                conn.execute(
                    "INSERT INTO pairs (polymarket_slug, kalshi_ticker, similarity_score, verified, created_at) "
                    "VALUES ('verified-but-undiscovered', 'K-X', 1.0, 1, 'now')"
                )
                conn.commit()
                conn.close()

                settings = _settings(db_path)
                with patch("bot.runner.PolymarketRestClient") as MockRest, \
                     patch("bot.runner.PolymarketWSClient") as MockWS, \
                     patch("bot.runner.KalshiFeedClient") as MockKalshi:
                    mock_rest = MockRest.return_value
                    mock_rest.discover_markets = AsyncMock(return_value=[])  # nothing to auto-fill from
                    mock_rest.discover_all_markets = AsyncMock(return_value=[])
                    mock_rest.aclose = AsyncMock()
                    fake_stream = FakeWSStream()
                    mock_ws = MockWS.return_value
                    mock_ws.stream = fake_stream
                    mock_kalshi = MockKalshi.return_value
                    mock_kalshi.poll = AsyncMock(side_effect=lambda *a, **kw: asyncio.sleep(3600))
                    mock_kalshi.get_series_list = AsyncMock(return_value=[])
                    mock_kalshi.get_markets_bulk = AsyncMock(return_value=[])

                    try:
                        await asyncio.wait_for(run(settings), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

                self.assertIn("verified-but-undiscovered", fake_stream.last_market_slugs)
            finally:
                db_module.connect = original_connect
                if os.path.exists(db_path):
                    os.remove(db_path)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
