from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

REQUIRED_ENV_VARS = ("POLYMARKET_API_KEY_ID", "POLYMARKET_PRIVATE_KEY")


class PolymarketFeedConfig(BaseModel):
    categories: list[str] = ["sports"]
    watchlist_slugs: list[str] = []
    max_markets_per_connection: int = 100
    rest_rate_limit_per_sec: int = 20
    discovery_refresh_seconds: int = 3600


class KalshiFeedConfig(BaseModel):
    enabled: bool = True
    poll_seconds: int = 15
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"


class FeedsConfig(BaseModel):
    polymarket: PolymarketFeedConfig
    kalshi: KalshiFeedConfig


class LogConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/bot.log"


class Settings(BaseModel):
    data_collection_enabled: bool = False
    feeds: FeedsConfig
    log: LogConfig
    polymarket_api_key_id: str
    polymarket_private_key: str


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
    )
