from __future__ import annotations

import json
import sqlite3

from bot.fees import taker_fee
from bot.pricing import consensus_devigged_prob_for_team, polymarket_best_bid_ask, polymarket_top_levels
from bot.state import MarketState
from bot.strategies.base import (
    Opportunity,
    close_opportunity,
    find_open_opportunity,
    hash_params,
    insert_opportunity,
    touch_opportunity,
    upsert_market_snapshot,
)
from bot.timeutil import now_iso

STRATEGY_ID = "sportsbook_divergence"


class SportsbookDivergenceStrategy:
    """De-vigged consensus odds (The Odds API) vs. Polymarket US executable price.

    Only acts on verified odds_pairs — same false-match risk as the Kalshi
    divergence strategy applies here too (team-name matching across venues).
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
            "SELECT id, polymarket_slug, odds_api_game_id, long_team, polymarket_question "
            "FROM odds_pairs WHERE verified = 1"
        ).fetchall()

        for pair_id, pm_slug, game_id, long_team, label in pairs:
            pm_book = self.state.polymarket_book(pm_slug)
            game = self.state.odds_api.get(game_id)
            existing = find_open_opportunity(self.conn, self.strategy_id, pm_slug)

            if not pm_book or not game:
                continue
            if self.state.polymarket_is_stale(pm_slug) or self.state.odds_api.is_stale(game_id):
                if existing is not None:
                    close_opportunity(self.conn, existing, now)
                continue

            pm_bid, pm_ask = polymarket_best_bid_ask(pm_book)
            consensus_prob = consensus_devigged_prob_for_team(game, long_team)
            if pm_bid is None or pm_ask is None or consensus_prob is None:
                continue

            buy_edge = consensus_prob - pm_ask - taker_fee(pm_ask)
            sell_edge = pm_bid - consensus_prob - taker_fee(pm_bid)

            upsert_market_snapshot(
                self.conn, self.strategy_id, str(pair_id), label, (pm_bid + pm_ask) / 2, consensus_prob,
                max(buy_edge, sell_edge) * 100, self.entry_threshold_cents, now,
            )

            direction, signal_value, entry_price = None, 0.0, None
            threshold = self.entry_threshold_cents / 100
            if buy_edge >= threshold and buy_edge >= sell_edge:
                direction, signal_value, entry_price = "buy_polymarket", buy_edge, pm_ask
            elif sell_edge >= threshold:
                direction, signal_value, entry_price = "sell_polymarket", sell_edge, pm_bid

            if direction:
                top_levels = {"polymarket": polymarket_top_levels(pm_book), "consensus_prob": consensus_prob}
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
