from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

REQUIRED_ENV_VARS = ("POLYMARKET_API_KEY_ID", "POLYMARKET_PRIVATE_KEY", "ODDS_API_KEY")


class PolymarketFeedConfig(BaseModel):
    # Server-side filter on the /v1/markets "category" field — every sports
    # market returns category="sports" regardless of league, so this only
    # ever separates sports from other top-level categories (politics/econ/
    # weather), never mlb from nba. Do not put league codes here.
    categories: list[str] = ["sports"]
    # Client-side filter (see bot.feeds.polymarket.league_of) on the league
    # code embedded in each market's slug — the only field that reliably
    # distinguishes mlb/nba/ufc/etc. Empty = no league filtering.
    leagues: list[str] = []
    watchlist_slugs: list[str] = []
    max_markets_per_connection: int = 100
    rest_rate_limit_per_sec: int = 20
    discovery_refresh_seconds: int = 3600
    # "ws" (default, for a normal network) or "rest_poll" — fallback for a
    # network that blocks/mangles the authenticated WS handshake.
    transport: str = "ws"
    rest_poll_seconds: float = 3.0


class KalshiFeedConfig(BaseModel):
    enabled: bool = True
    poll_seconds: int = 15
    # external-api.kalshi.com is the current recommended production host;
    # api.elections.kalshi.com / trading-api.kalshi.com are older forms still
    # referenced in some docs. Verified 2026-08-09 (docs.kalshi.com).
    base_url: str = "https://external-api.kalshi.com/trade-api/v2"


class OddsApiFeedConfig(BaseModel):
    enabled: bool = True
    poll_seconds: int = 30
    base_url: str = "https://api.the-odds-api.com"
    regions: str = "us"


class FeedsConfig(BaseModel):
    polymarket: PolymarketFeedConfig
    kalshi: KalshiFeedConfig
    odds_api: OddsApiFeedConfig


class LogConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/bot.log"


class KalshiDivergenceStrategyConfig(BaseModel):
    enabled: bool = True
    entry_threshold_cents: float = 4.0
    fill_timeout_seconds: int = 60


class SportsbookDivergenceStrategyConfig(BaseModel):
    enabled: bool = True
    entry_threshold_cents: float = 4.0
    fill_timeout_seconds: int = 60


class LockedPairArbStrategyConfig(BaseModel):
    enabled: bool = True
    min_abs_edge_cents: float = 1.0
    hurdle_annual_return: float = 0.25
    min_viable_size: int = 1
    max_size_per_pair: int = 50
    notional_usd_cap: float = 50
    settlement_lag_days: float = 2.0
    # Risk model (build prompt §5.3) — deliberately pessimistic starting
    # priors, not calibrated. Calibrate p_divergence per category from
    # settlements once enough have accumulated (see `bot report`).
    p_divergence: float = 0.02
    asymmetry: float = 0.85


class StrategiesConfig(BaseModel):
    kalshi_divergence: KalshiDivergenceStrategyConfig
    sportsbook_divergence: SportsbookDivergenceStrategyConfig
    locked_pair_arb: LockedPairArbStrategyConfig


class Settings(BaseModel):
    data_collection_enabled: bool = False
    feeds: FeedsConfig
    strategies: StrategiesConfig
    log: LogConfig
    position_notional_usd: float = 10
    max_concurrent_positions: int = 5
    daily_sim_loss_stop_usd: float = 50
    polymarket_api_key_id: str
    polymarket_private_key: str
    odds_api_key: str


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    load_dotenv()

    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in before starting the bot."
        )

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    return Settings(
        **raw,
        polymarket_api_key_id=values["POLYMARKET_API_KEY_ID"],
        polymarket_private_key=values["POLYMARKET_PRIVATE_KEY"],
        odds_api_key=values["ODDS_API_KEY"],
    )
