from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


def _joined(value: str | list[str]) -> str:
    return value if isinstance(value, str) else ",".join(value)


class OddsApiFeedClient:
    """Read-only client for The Odds API (api.the-odds-api.com/v4).

    Quota is a monthly credit budget (cost = markets x regions per call),
    not a per-second rate limit, so no token bucket here — see
    bot.odds_api_budget for the client-side monthly cap that replaces it.
    """

    def __init__(
        self, api_key: str, base_url: str, regions: str | list[str] = "us",
        markets: str | list[str] = "h2h", timeout: float = 10.0,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self.api_key = api_key
        self.regions = _joined(regions)
        self.markets = _joined(markets)

    async def get_odds(
        self, sport_key: str, markets: str | None = None,
        on_headers: Callable[[httpx.Headers], None] | None = None,
    ) -> list[dict]:
        resp = await self._client.get(
            f"/v4/sports/{sport_key}/odds/",
            params={
                "apiKey": self.api_key, "regions": self.regions,
                "markets": markets or self.markets, "oddsFormat": "decimal",
            },
        )
        # Credits headers (x-requests-remaining/x-requests-used) are present
        # on error responses too — confirmed live against an exhausted-quota
        # 401 (docs/ODDS_API_AUDIT.md) — so this must run before
        # raise_for_status(), not after.
        if on_headers:
            on_headers(resp.headers)
        resp.raise_for_status()
        return resp.json()

    async def poll(
        self,
        sport_keys: list[str],
        on_update: Callable[[str, list[dict]], Awaitable[None]],
        poll_seconds: int,
        stop_event: asyncio.Event | None = None,
        on_success: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_headers: Callable[[httpx.Headers], None] | None = None,
        should_call: Callable[[], bool] | None = None,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            for sport_key in sport_keys:
                if should_call is not None and not should_call():
                    # Monthly credit budget exhausted (bot.odds_api_budget) —
                    # stop calling entirely rather than let the API 401 us;
                    # this is a deliberate skip, not a feed error.
                    logger.info("Odds API call skipped for %s: monthly credit budget exhausted", sport_key)
                    continue
                try:
                    games = await self.get_odds(sport_key, on_headers=on_headers)
                    await on_update(sport_key, games)
                    if on_success:
                        on_success()
                except httpx.HTTPError as exc:
                    logger.warning("Odds API poll failed for %s: %s", sport_key, exc)
                    if on_error:
                        on_error(str(exc))
            await asyncio.sleep(poll_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()
