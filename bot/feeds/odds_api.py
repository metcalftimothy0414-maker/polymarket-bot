from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


class OddsApiFeedClient:
    """Read-only client for The Odds API (api.the-odds-api.com/v4).

    Quota is a monthly credit budget (cost = markets x regions per call),
    not a per-second rate limit, so no token bucket here.
    """

    def __init__(self, api_key: str, base_url: str, regions: str = "us", timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self.api_key = api_key
        self.regions = regions

    async def get_odds(self, sport_key: str, markets: str = "h2h") -> list[dict]:
        resp = await self._client.get(
            f"/v4/sports/{sport_key}/odds/",
            params={"apiKey": self.api_key, "regions": self.regions, "markets": markets, "oddsFormat": "decimal"},
        )
        resp.raise_for_status()
        return resp.json()

    async def poll(
        self,
        sport_keys: list[str],
        on_update: Callable[[str, list[dict]], Awaitable[None]],
        poll_seconds: int,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            for sport_key in sport_keys:
                try:
                    games = await self.get_odds(sport_key)
                    await on_update(sport_key, games)
                except httpx.HTTPError as exc:
                    logger.warning("Odds API poll failed for %s: %s", sport_key, exc)
            await asyncio.sleep(poll_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()
