from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal

from bot import db
from bot.config import Settings
from bot.edge import RiskParams
from bot.fee_multipliers import daily_refresh_loop as fee_multiplier_refresh_loop
from bot.feeds.auth import PolymarketAuth
from bot.feeds.kalshi import KalshiFeedClient
from bot.feeds.odds_api import OddsApiFeedClient
from bot.feeds.polymarket import PolymarketRestClient, PolymarketRestPoller, PolymarketWSClient, filter_by_leagues
from bot.paper import (
    FillSimulator,
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
from bot.strategies.locked_pair_arb import LockedPairArbStrategy
from bot.strategies.locked_pair_arb.settlement import resolve_pair_positions
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
    markets = filter_by_leagues(markets, settings.feeds.polymarket.leagues)
    _store_market_metadata(conn, markets)
    if not watchlist:
        watchlist = [m["slug"] for m in markets[: settings.feeds.polymarket.max_markets_per_connection]]

    # A verified pair is useless to Strategy A/B without live book data for its
    # Polymarket side — union it in regardless of how the rest of the watchlist
    # was built, so "verify a pair" alone is enough to make it trade-eligible.
    verified_pm_slugs = {
        r[0] for r in conn.execute("SELECT DISTINCT polymarket_slug FROM pairs WHERE verified = 1")
    } | {
        r[0] for r in conn.execute("SELECT DISTINCT polymarket_slug FROM odds_pairs WHERE verified = 1")
    }
    watchlist = list(dict.fromkeys([*verified_pm_slugs, *watchlist]))[: settings.feeds.polymarket.max_markets_per_connection]

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
    if settings.strategies.kalshi_divergence.enabled:
        s = DivergenceStrategy(conn, state, settings.strategies.kalshi_divergence.entry_threshold_cents)
        strategies.append(s)
        fill_timeouts[s.strategy_id] = settings.strategies.kalshi_divergence.fill_timeout_seconds
    if settings.strategies.sportsbook_divergence.enabled:
        s = SportsbookDivergenceStrategy(conn, state, settings.strategies.sportsbook_divergence.entry_threshold_cents)
        strategies.append(s)
        fill_timeouts[s.strategy_id] = settings.strategies.sportsbook_divergence.fill_timeout_seconds

    locked_pair_arb_strategy = None
    lpa = settings.strategies.locked_pair_arb
    if lpa.enabled:
        locked_pair_arb_strategy = LockedPairArbStrategy(
            conn, state,
            min_abs_edge=Decimal(str(lpa.min_abs_edge_cents / 100)),
            hurdle_annual_return=Decimal(str(lpa.hurdle_annual_return)),
            min_viable_size=lpa.min_viable_size,
            max_size_per_pair=lpa.max_size_per_pair,
            notional_usd_cap=Decimal(str(lpa.notional_usd_cap)),
            settlement_lag_days=Decimal(str(lpa.settlement_lag_days)),
            risk=RiskParams(p_divergence=Decimal(str(lpa.p_divergence)), asymmetry=Decimal(str(lpa.asymmetry))),
        )

    background_tasks = [polymarket_task]

    kalshi_client = None
    if settings.feeds.kalshi.enabled:
        # Always create the client when Kalshi is enabled — the fee
        # multiplier refresh and (eventually) catalog discovery need it
        # regardless of whether any pair is verified yet, not just the
        # per-ticker orderbook poll below.
        kalshi_client = KalshiFeedClient(settings.feeds.kalshi.base_url, settings.feeds.kalshi.poll_seconds)
        background_tasks.append(asyncio.create_task(
            fee_multiplier_refresh_loop(kalshi_client, conn, stop_event)
        ))

        kalshi_tickers = [r[0] for r in conn.execute("SELECT DISTINCT kalshi_ticker FROM pairs WHERE verified = 1")]
        if kalshi_tickers:
            async def on_kalshi_update(ticker: str, book: dict) -> None:
                state.kalshi.update(ticker, book)

            background_tasks.append(asyncio.create_task(
                kalshi_client.poll(kalshi_tickers, on_kalshi_update, stop_event)
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
        locked_pair_arb_strategy, kalshi_client,
    )))
    background_tasks.append(asyncio.create_task(_heartbeat_loop(conn, stop_event)))

    try:
        await asyncio.gather(*background_tasks)
    finally:
        await rest.aclose()


async def _on_polymarket_update(_data: dict) -> None:
    pass  # MarketState reads straight from ws.books; nothing extra to do per-tick


async def _scan_loop(
    conn, state, strategies, fill_simulator, fill_timeouts, rest, settings, stop_event,
    locked_pair_arb_strategy=None, kalshi_client=None,
) -> None:
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

            if locked_pair_arb_strategy is not None:
                # Self-contained: unlike A/B above, scan() both detects and
                # opens (Mode A fires against the book it just walked — see
                # bot/strategies/locked_pair_arb/__init__.py) so there's no
                # generic fill_simulator step here.
                await locked_pair_arb_strategy.scan()
                if kalshi_client is not None:
                    await resolve_pair_positions(conn, kalshi_client, rest, settings.feeds.polymarket.categories)

            close_positions_for_closed_opportunities(conn, state)
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
