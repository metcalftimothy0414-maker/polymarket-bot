from __future__ import annotations

import datetime as dt
import json
import sqlite3
import statistics

from bot.pricing import (
    polymarket_best_bid_ask,
    polymarket_book_depth_usd,
    polymarket_mid_price,
    polymarket_top_levels,
    trade_size_usd,
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

STRATEGY_ID = "large_flow"

# Below this many prior prints, "10x the median" isn't a meaningful baseline yet
# (e.g. a single $50 trade would make the very next $600 trade look like a 12x
# outlier purely from small-sample noise).
MIN_TRADE_SAMPLES = 5
SIZE_HISTORY_WINDOW = 50


def _parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class LargeFlowStrategy:
    """Follows abnormally large trades on the public Polymarket US trade feed.

    Every print on that feed is taker-initiated — it matched against a resting
    maker order, i.e. it crossed the spread by construction — so "aggressive"
    here reduces entirely to size, not a separate maker/taker classification.
    Single-venue like Strategy C: same watchlist, same mechanical filters.
    """

    strategy_id = STRATEGY_ID

    def __init__(
        self,
        conn: sqlite3.Connection,
        state: MarketState,
        market_slugs: list[str],
        size_multiple_threshold: float = 10.0,
        max_spread_cents: float = 3,
        min_implied_prob: float = 0.40,
        max_implied_prob: float = 0.60,
        min_depth_usd: float = 500,
    ) -> None:
        self.conn = conn
        self.state = state
        self.market_slugs = market_slugs
        self.size_multiple_threshold = size_multiple_threshold
        self.max_spread_cents = max_spread_cents
        self.min_implied_prob = min_implied_prob
        self.max_implied_prob = max_implied_prob
        self.min_depth_usd = min_depth_usd
        self._size_history: dict[str, list[float]] = {}
        self._last_trade_time: dict[str, str] = {}

    def params_hash(self) -> str:
        return hash_params({
            "size_multiple_threshold": self.size_multiple_threshold,
            "max_spread_cents": self.max_spread_cents,
            "min_implied_prob": self.min_implied_prob,
            "max_implied_prob": self.max_implied_prob,
            "min_depth_usd": self.min_depth_usd,
        })

    def _market_row(self, slug: str) -> tuple | None:
        return self.conn.execute(
            "SELECT game_start_time, end_date, closed FROM markets WHERE slug = ?", (slug,)
        ).fetchone()

    def _seconds_remaining(self, row: tuple | None, now_dt: dt.datetime) -> float | None:
        if row is None:
            return None
        _start, end_date, _closed = row
        return None if not end_date else (_parse_iso(end_date) - now_dt).total_seconds()

    def _consume_new_trades(self, slug: str) -> tuple[float | None, float | None, dict | None]:
        """Rolls the per-slug size history forward past every trade seen since the
        last scan tick and returns the strongest (highest-multiple) signal among
        them, if any: (size_multiple, trade_size_usd, raw_trade)."""
        trades = sorted(self.state.polymarket_trades(slug), key=lambda t: t["tradeTime"])
        cursor = self._last_trade_time.get(slug, "")
        new_trades = [t for t in trades if t["tradeTime"] > cursor]
        if not new_trades:
            return None, None, None

        history = self._size_history.setdefault(slug, [])
        best: tuple[float, float, dict] | None = None
        for trade in new_trades:
            size = trade_size_usd(trade)
            median = statistics.median(history) if len(history) >= MIN_TRADE_SAMPLES else None
            if median:
                multiple = size / median
                if best is None or multiple > best[0]:
                    best = (multiple, size, trade)
            history.append(size)
        del history[:-SIZE_HISTORY_WINDOW]

        self._last_trade_time[slug] = new_trades[-1]["tradeTime"]
        return best if best else (None, None, None)

    async def scan(self) -> list[Opportunity]:
        now_dt = dt.datetime.now(dt.timezone.utc)
        now = now_dt.isoformat()
        detected: list[Opportunity] = []

        for slug in self.market_slugs:
            size_multiple, size_usd, trade = self._consume_new_trades(slug)

            pm_book = self.state.polymarket_book(slug)
            existing = find_open_opportunity(self.conn, self.strategy_id, slug)
            if not pm_book or self.state.polymarket_is_stale(slug):
                if existing is not None:
                    close_opportunity(self.conn, existing, now)
                continue

            pm_bid, pm_ask = polymarket_best_bid_ask(pm_book)
            if pm_bid is None or pm_ask is None:
                continue
            mid = (pm_bid + pm_ask) / 2
            spread_cents = (pm_ask - pm_bid) * 100
            row = self._market_row(slug)
            seconds_remaining = self._seconds_remaining(row, now_dt)

            mechanical_filters_pass = (
                spread_cents <= self.max_spread_cents
                and self.min_implied_prob <= mid <= self.max_implied_prob
                and seconds_remaining is not None and seconds_remaining > 0
            )

            direction, entry_price, depth_usd = None, None, 0.0
            if trade is not None and size_multiple >= self.size_multiple_threshold:
                taker_action = trade["taker"]["action"]
                if taker_action == "ORDER_ACTION_BUY":
                    depth_usd = polymarket_book_depth_usd(pm_book, "offers")
                    if mechanical_filters_pass and depth_usd >= self.min_depth_usd:
                        direction, entry_price = "buy_polymarket", pm_ask
                elif taker_action == "ORDER_ACTION_SELL":
                    depth_usd = polymarket_book_depth_usd(pm_book, "bids")
                    if mechanical_filters_pass and depth_usd >= self.min_depth_usd:
                        direction, entry_price = "sell_polymarket", pm_bid

            # Skip-log every candidate (traded or not) — including ticks with no
            # qualifying trade at all, so offline analysis sees the full picture.
            self.conn.execute(
                "INSERT INTO candidates (strategy_id, market_ref, ts, implied_prob, momentum_value, "
                "spread_cents, depth_usd, seconds_remaining, traded, trade_size_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.strategy_id, slug, now, mid, size_multiple, spread_cents, depth_usd,
                 seconds_remaining, int(direction is not None), size_usd),
            )

            if direction:
                top_levels = polymarket_top_levels(pm_book)
                if existing is None:
                    opp_id = insert_opportunity(
                        self.conn, self.strategy_id, self.params_hash(), None, slug,
                        direction, size_multiple, entry_price, top_levels, now,
                    )
                else:
                    touch_opportunity(self.conn, existing["id"], now)
                    opp_id = existing["id"]
                detected.append(Opportunity(
                    strategy_id=self.strategy_id,
                    params_hash=self.params_hash(),
                    detected_at=now,
                    market_ref=slug,
                    direction=direction,
                    signal_value=size_multiple,
                    opportunity_id=opp_id,
                    entry_price=entry_price,
                    top_levels_json=json.dumps(top_levels),
                ))
            elif existing is not None:
                close_opportunity(self.conn, existing, now)

        self._update_counterfactuals(now_dt)
        self.conn.commit()
        return detected

    def _update_counterfactuals(self, now_dt: dt.datetime) -> None:
        rows = self.conn.execute(
            "SELECT id, market_ref, detected_at, counterfactual_60s, counterfactual_300s FROM opportunities "
            "WHERE strategy_id = ? AND (counterfactual_60s IS NULL OR counterfactual_300s IS NULL)",
            (self.strategy_id,),
        ).fetchall()
        for opp_id, slug, detected_at, cf60, cf300 in rows:
            elapsed = (now_dt - _parse_iso(detected_at)).total_seconds()
            book = self.state.polymarket_book(slug)
            mid = polymarket_mid_price(book) if book else None
            if mid is None:
                continue
            if cf60 is None and elapsed >= 60:
                self.conn.execute("UPDATE opportunities SET counterfactual_60s = ? WHERE id = ?", (mid, opp_id))
            if cf300 is None and elapsed >= 300:
                self.conn.execute("UPDATE opportunities SET counterfactual_300s = ? WHERE id = ?", (mid, opp_id))
