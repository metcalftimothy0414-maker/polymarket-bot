from __future__ import annotations

import json
import sqlite3

from bot.fees import taker_fee
from bot.pricing import (
    kalshi_mid_price,
    kalshi_top_levels,
    polymarket_best_bid_ask,
    polymarket_top_levels,
)
from bot.state import MarketState
from bot.strategies.base import (
    Opportunity,
    close_opportunity,
    find_open_opportunity,
    hash_params,
    insert_opportunity,
    touch_opportunity,
)
from bot.timeutil import now_iso

STRATEGY_ID = "divergence"


class DivergenceStrategy:
    """Cross-venue signal: Kalshi mid-price vs. Polymarket US executable price.

    Only acts on verified pairs (matching/matcher.py) — never scans unverified
    ones, since a false pair looks like free money and is actually a coin flip.
    """

    strategy_id = STRATEGY_ID

    def __init__(
        self,
        conn: sqlite3.Connection,
        state: MarketState,
        entry_threshold_cents: float = 4.0,
    ) -> None:
        self.conn = conn
        self.state = state
        self.entry_threshold_cents = entry_threshold_cents

    def params_hash(self) -> str:
        return hash_params({"entry_threshold_cents": self.entry_threshold_cents})

    async def scan(self) -> list[Opportunity]:
        now = now_iso()
        detected: list[Opportunity] = []

        pairs = self.conn.execute(
            "SELECT id, polymarket_slug, kalshi_ticker FROM pairs WHERE verified = 1"
        ).fetchall()

        for pair_id, pm_slug, k_ticker in pairs:
            pm_book = self.state.polymarket_book(pm_slug)
            k_book = self.state.kalshi.get(k_ticker)
            existing = find_open_opportunity(self.conn, self.strategy_id, pm_slug)

            if not pm_book or not k_book:
                continue
            # Hard risk rule: never trade on stale data — a stale-data "edge" is fake.
            if self.state.polymarket_is_stale(pm_slug) or self.state.kalshi.is_stale(k_ticker):
                if existing is not None:
                    close_opportunity(self.conn, existing, now)
                continue

            pm_bid, pm_ask = polymarket_best_bid_ask(pm_book)
            k_mid = kalshi_mid_price(k_book)
            if pm_bid is None or pm_ask is None or k_mid is None:
                continue

            # Executable divergence: what we could actually fill at, fee-adjusted.
            buy_edge = k_mid - pm_ask - taker_fee(pm_ask)
            sell_edge = pm_bid - k_mid - taker_fee(pm_bid)

            direction, signal_value, entry_price = None, 0.0, None
            threshold = self.entry_threshold_cents / 100
            if buy_edge >= threshold and buy_edge >= sell_edge:
                direction, signal_value, entry_price = "buy_polymarket", buy_edge, pm_ask
            elif sell_edge >= threshold:
                direction, signal_value, entry_price = "sell_polymarket", sell_edge, pm_bid

            if direction:
                top_levels = {
                    "polymarket": polymarket_top_levels(pm_book),
                    "kalshi": kalshi_top_levels(k_book),
                }
                if existing is None:
                    opp_id = insert_opportunity(
                        self.conn, self.strategy_id, self.params_hash(), pair_id, pm_slug,
                        direction, signal_value, entry_price, top_levels, now,
                    )
                else:
                    touch_opportunity(self.conn, existing["id"], now)
                    opp_id = existing["id"]
                detected.append(Opportunity(
                    strategy_id=self.strategy_id,
                    params_hash=self.params_hash(),
                    detected_at=now,
                    market_ref=pm_slug,
                    direction=direction,
                    signal_value=signal_value,
                    opportunity_id=opp_id,
                    entry_price=entry_price,
                    top_levels_json=json.dumps(top_levels),
                ))
            elif existing is not None:
                close_opportunity(self.conn, existing, now)

        self.conn.commit()
        return detected
