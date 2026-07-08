from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

REQUIRED_ENV_VARS = ("POLYMARKET_API_KEY_ID", "POLYMARKET_PRIVATE_KEY", "ODDS_API_KEY")


class PolymarketFeedConfig(BaseModel):
    categories: list[str] = ["sports"]
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
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"


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


class SportsMomentumStrategyConfig(BaseModel):
    enabled: bool = True
    momentum_lookback_seconds: float = 120
    momentum_threshold_cents: float = 3
    exit_reversal_cents: float = 4
    max_spread_cents: float = 3
    min_implied_prob: float = 0.40
    max_implied_prob: float = 0.60
    min_depth_usd: float = 500
    fill_timeout_seconds: int = 10


class StrategiesConfig(BaseModel):
    kalshi_divergence: KalshiDivergenceStrategyConfig
    sportsbook_divergence: SportsbookDivergenceStrategyConfig
    sports_momentum: SportsMomentumStrategyConfig


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
