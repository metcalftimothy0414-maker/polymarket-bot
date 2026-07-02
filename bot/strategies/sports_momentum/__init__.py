from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time

from bot.pricing import polymarket_best_bid_ask, polymarket_book_depth_usd, polymarket_mid_price, polymarket_top_levels
from bot.state import MarketState
from bot.strategies.base import (
    Opportunity,
    close_opportunity,
    find_open_opportunity,
    hash_params,
    insert_opportunity,
    touch_opportunity,
)

STRATEGY_ID = "sports_momentum"


def _parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class SportsMomentumStrategy:
    """Short-window contract-price momentum on live Polymarket US sports markets.

    Single-venue (no cross-venue pair, no false-match risk) — universe is
    whatever slugs the caller passes in (same watchlist Strategies A/B track).
    Signal computes purely from cached WS book + an in-memory price history
    buffer, no network round-trip, so it's well within the <100ms budget.
    """

    strategy_id = STRATEGY_ID

    def __init__(
        self,
        conn: sqlite3.Connection,
        state: MarketState,
        market_slugs: list[str],
        momentum_lookback_seconds: float = 120,
        momentum_threshold_cents: float = 3,
        exit_reversal_cents: float = 4,
        max_spread_cents: float = 3,
        min_implied_prob: float = 0.40,
        max_implied_prob: float = 0.60,
        min_depth_usd: float = 500,
    ) -> None:
        self.conn = conn
        self.state = state
        self.market_slugs = market_slugs
        self.momentum_lookback_seconds = momentum_lookback_seconds
        self.momentum_threshold_cents = momentum_threshold_cents
        self.exit_reversal_cents = exit_reversal_cents  # consumed by paper/ on exit, not enforced here
        self.max_spread_cents = max_spread_cents
        self.min_implied_prob = min_implied_prob
        self.max_implied_prob = max_implied_prob
        self.min_depth_usd = min_depth_usd
        self._price_history: dict[str, list[tuple[float, float]]] = {}

    def params_hash(self) -> str:
        return hash_params({
            "momentum_lookback_seconds": self.momentum_lookback_seconds,
            "momentum_threshold_cents": self.momentum_threshold_cents,
            "exit_reversal_cents": self.exit_reversal_cents,
            "max_spread_cents": self.max_spread_cents,
            "min_implied_prob": self.min_implied_prob,
            "max_implied_prob": self.max_implied_prob,
            "min_depth_usd": self.min_depth_usd,
        })

    def _record_price(self, slug: str, mid: float, now_mono: float) -> None:
        hist = self._price_history.setdefault(slug, [])
        hist.append((now_mono, mid))
        cutoff = now_mono - self.momentum_lookback_seconds - 5
        while hist and hist[0][0] < cutoff:
            hist.pop(0)

    def _momentum(self, slug: str, now_mono: float, mid_now: float) -> float | None:
        hist = self._price_history.get(slug, [])
        target = now_mono - self.momentum_lookback_seconds
        past = None
        for ts, price in hist:
            if ts <= target:
                past = price
            else:
                break
        return None if past is None else mid_now - past

    def _market_row(self, slug: str) -> tuple | None:
        return self.conn.execute(
            "SELECT game_start_time, end_date, closed FROM markets WHERE slug = ?", (slug,)
        ).fetchone()

    def _in_progress(self, row: tuple | None, now_dt: dt.datetime) -> bool:
        if row is None:
            return False
        game_start_time, _end_date, closed = row
        if closed or not game_start_time:
            return False
        return now_dt >= _parse_iso(game_start_time)

    def _seconds_remaining(self, row: tuple | None, now_dt: dt.datetime) -> float | None:
        if row is None:
            return None
        _start, end_date, _closed = row
        return None if not end_date else (_parse_iso(end_date) - now_dt).total_seconds()

    async def scan(self) -> list[Opportunity]:
        now_dt = dt.datetime.now(dt.timezone.utc)
        now = now_dt.isoformat()
        now_mono = time.monotonic()
        detected: list[Opportunity] = []

        for slug in self.market_slugs:
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
            self._record_price(slug, mid, now_mono)

            momentum = self._momentum(slug, now_mono, mid)
            if momentum is None:
                continue  # not enough history yet — nothing meaningful to log either

            spread_cents = (pm_ask - pm_bid) * 100
            row = self._market_row(slug)
            seconds_remaining = self._seconds_remaining(row, now_dt)
            in_progress = self._in_progress(row, now_dt)

            mechanical_filters_pass = (
                spread_cents <= self.max_spread_cents
                and self.min_implied_prob <= mid <= self.max_implied_prob
                and in_progress
                and seconds_remaining is not None and seconds_remaining > 0
            )

            direction, entry_price, depth_usd = None, None, 0.0
            if momentum > 0:
                depth_usd = polymarket_book_depth_usd(pm_book, "offers")
                if (mechanical_filters_pass and depth_usd >= self.min_depth_usd
                        and momentum * 100 >= self.momentum_threshold_cents):
                    direction, entry_price = "buy_polymarket", pm_ask
            elif momentum < 0:
                depth_usd = polymarket_book_depth_usd(pm_book, "bids")
                if (mechanical_filters_pass and depth_usd >= self.min_depth_usd
                        and -momentum * 100 >= self.momentum_threshold_cents):
                    direction, entry_price = "sell_polymarket", pm_bid

            # Skip-log every candidate (traded or not) so offline analysis can check
            # whether any parameter region showed edge, not just the one we picked.
            self.conn.execute(
                "INSERT INTO candidates (strategy_id, market_ref, ts, implied_prob, momentum_value, "
                "spread_cents, depth_usd, seconds_remaining, traded) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.strategy_id, slug, now, mid, momentum, spread_cents, depth_usd,
                 seconds_remaining, int(direction is not None)),
            )

            if direction:
                top_levels = polymarket_top_levels(pm_book)
                if existing is None:
                    opp_id = insert_opportunity(
                        self.conn, self.strategy_id, self.params_hash(), None, slug,
                        direction, momentum, entry_price, top_levels, now,
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
                    signal_value=momentum,
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
