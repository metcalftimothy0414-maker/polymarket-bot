from __future__ import annotations

import asyncio
import datetime as dt
import logging

from bot import db
from bot.config import Settings
from bot.feeds.auth import PolymarketAuth
from bot.feeds.kalshi import KalshiFeedClient
from bot.feeds.odds_api import OddsApiFeedClient
from bot.feeds.polymarket import PolymarketRestClient, PolymarketRestPoller, PolymarketWSClient
from bot.paper import (
    FillSimulator,
    check_reversal_exits,
    close_positions_for_closed_opportunities,
    count_open_positions,
    daily_realized_pnl,
    has_open_trade_for_opportunity,
    open_position,
    record_unfilled,
    resolve_closed_markets,
)
from bot.state import MarketState
from bot.strategies.divergence import DivergenceStrategy
from bot.strategies.sports_momentum import SportsMomentumStrategy
from bot.strategies.sportsbook_divergence import SportsbookDivergenceStrategy

logger = logging.getLogger(__name__)
SCAN_INTERVAL_SECONDS = 5
HEARTBEAT_INTERVAL_SECONDS = 300


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _store_market_metadata(conn, markets: list[dict]) -> None:
    now = _now()
    for m in markets:
        conn.execute(
            "INSERT OR REPLACE INTO markets "
            "(slug, question, category, end_date, game_start_time, active, closed, raw_json, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "COALESCE((SELECT discovered_at FROM markets WHERE slug = ?), ?), ?)",
            (m["slug"], m.get("question"), m.get("category"), m.get("endDate"), m.get("gameStartTime"),
             int(m["active"]), int(m["closed"]), "", m["slug"], now, now),
        )
    conn.commit()


async def run(settings: Settings, allow_live: bool = False) -> None:
    if not settings.data_collection_enabled and not allow_live:
        print(
            "data_collection_enabled is false in config.yaml — the bot does nothing locally by design. "
            "Pass --allow-live for a one-off manual run, or set the flag true (VPS only)."
        )
        return

    conn = db.connect()
    rest = PolymarketRestClient(settings.feeds.polymarket.rest_rate_limit_per_sec)

    watchlist = settings.feeds.polymarket.watchlist_slugs
    markets = await rest.discover_markets(settings.feeds.polymarket.categories, closed=False)
    _store_market_metadata(conn, markets)
    if not watchlist:
        watchlist = [m["slug"] for m in markets[: settings.feeds.polymarket.max_markets_per_connection]]

    stop_event = asyncio.Event()
    use_rest_poll = settings.feeds.polymarket.transport == "rest_poll"
    if use_rest_poll:
        book_source = PolymarketRestPoller(rest, settings.feeds.polymarket.rest_poll_seconds)
        polymarket_task = asyncio.create_task(book_source.poll(watchlist, stop_event))
    else:
        auth = PolymarketAuth(settings.polymarket_api_key_id, settings.polymarket_private_key)
        book_source = PolymarketWSClient(auth)
        polymarket_task = asyncio.create_task(book_source.stream(watchlist, _on_polymarket_update, stop_event=stop_event))

    state = MarketState(book_source)
    fill_simulator = FillSimulator(state)

    strategies = []
    fill_timeouts: dict[str, int] = {}
    if settings.strategies.divergence.enabled:
        s = DivergenceStrategy(conn, state, settings.strategies.divergence.entry_threshold_cents)
        strategies.append(s)
        fill_timeouts[s.strategy_id] = settings.strategies.divergence.fill_timeout_seconds
    if settings.strategies.sportsbook_divergence.enabled:
        s = SportsbookDivergenceStrategy(conn, state, settings.strategies.sportsbook_divergence.entry_threshold_cents)
        strategies.append(s)
        fill_timeouts[s.strategy_id] = settings.strategies.sportsbook_divergence.fill_timeout_seconds
    if settings.strategies.sports_momentum.enabled:
        smc = settings.strategies.sports_momentum
        s = SportsMomentumStrategy(
            conn, state, watchlist, smc.momentum_lookback_seconds, smc.momentum_threshold_cents,
            smc.exit_reversal_cents, smc.max_spread_cents, smc.min_implied_prob, smc.max_implied_prob, smc.min_depth_usd,
        )
        strategies.append(s)
        fill_timeouts[s.strategy_id] = smc.fill_timeout_seconds

    background_tasks = [polymarket_task]

    if settings.feeds.kalshi.enabled:
        kalshi_tickers = [r[0] for r in conn.execute("SELECT DISTINCT kalshi_ticker FROM pairs WHERE verified = 1")]
        if kalshi_tickers:
            kalshi_client = KalshiFeedClient(settings.feeds.kalshi.base_url, settings.feeds.kalshi.poll_seconds)

            async def on_kalshi_update(ticker: str, book: dict) -> None:
                state.kalshi.update(ticker, book)

            background_tasks.append(asyncio.create_task(
                kalshi_client.poll(kalshi_tickers, on_kalshi_update, settings.feeds.kalshi.poll_seconds, stop_event)
            ))

    if settings.feeds.odds_api.enabled:
        odds_sport_keys = [r[0] for r in conn.execute("SELECT DISTINCT odds_api_sport_key FROM odds_pairs WHERE verified = 1")]
        if odds_sport_keys:
            odds_client = OddsApiFeedClient(settings.odds_api_key, settings.feeds.odds_api.base_url, settings.feeds.odds_api.regions)

            async def on_odds_update(sport_key: str, games: list[dict]) -> None:
                for game in games:
                    state.odds_api.update(game["id"], game)

            background_tasks.append(asyncio.create_task(
                odds_client.poll(odds_sport_keys, on_odds_update, settings.feeds.odds_api.poll_seconds, stop_event)
            ))

    background_tasks.append(asyncio.create_task(_scan_loop(
        conn, state, strategies, fill_simulator, fill_timeouts, rest, settings, stop_event,
    )))
    background_tasks.append(asyncio.create_task(_heartbeat_loop(conn, stop_event)))

    try:
        await asyncio.gather(*background_tasks)
    finally:
        await rest.aclose()


async def _on_polymarket_update(_data: dict) -> None:
    pass  # MarketState reads straight from ws.books; nothing extra to do per-tick


async def _scan_loop(conn, state, strategies, fill_simulator, fill_timeouts, rest, settings, stop_event) -> None:
    while not stop_event.is_set():
        try:
            for strategy in strategies:
                if daily_realized_pnl(conn, strategy.strategy_id) <= -settings.daily_sim_loss_stop_usd:
                    continue  # this strategy is done for the day; the others keep running

                opportunities = await strategy.scan()
                for opp in opportunities:
                    if count_open_positions(conn) >= settings.max_concurrent_positions:
                        break
                    if has_open_trade_for_opportunity(conn, opp.opportunity_id):
                        continue
                    timeout = fill_timeouts[strategy.strategy_id]
                    filled, fill_price = await fill_simulator.try_fill(opp, timeout)
                    if filled:
                        open_position(conn, opp, fill_price, settings.position_notional_usd)
                    else:
                        record_unfilled(conn, opp, settings.position_notional_usd)

            close_positions_for_closed_opportunities(conn, state)
            momentum_cfg = settings.strategies.sports_momentum
            if momentum_cfg.enabled:
                check_reversal_exits(conn, state, momentum_cfg.exit_reversal_cents, "sports_momentum")
            await resolve_closed_markets(conn, rest, settings.feeds.polymarket.categories)
        except Exception:
            logger.exception("scan loop iteration failed")
            conn.execute(
                "INSERT INTO errors (ts, component, message) VALUES (?, 'scan_loop', ?)",
                (_now(), "scan loop iteration failed, see log"),
            )
            conn.commit()
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def _heartbeat_loop(conn, stop_event) -> None:
    while not stop_event.is_set():
        conn.execute("INSERT INTO heartbeats (ts, component, detail) VALUES (?, 'runner', 'alive')", (_now(),))
        conn.commit()
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
