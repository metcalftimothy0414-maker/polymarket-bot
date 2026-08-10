"""Type 3 locked-pair arbitrage (build prompt §4): buy YES on the cheap venue
+ NO on the other, only entering when the risk-adjusted edge clears a
hurdle, and holding to settlement rather than exiting early.

Unlike kalshi_divergence/sportsbook_divergence (single-venue directional
entry on a cross-venue signal, exit on signal-close), this strategy only
ever opens a position with BOTH legs confirmed in the same pass — see
docs/DECISIONS.md #6: this phase is Mode A (simultaneous-taker) only, no
resting-and-hoping-to-hedge, which sidesteps hedge-fail risk entirely at
the cost of being the lower-EV execution mode (build prompt §3.5).

ponytail: because Mode A fires against currently-visible depth (IOC-style,
not a resting order), the executable-depth book walk over the *current*
book snapshot doubles as the fill simulation — there's no separate polling
loop like FillSimulator uses for the resting-limit strategies. Real-world
latency between "walked this book" and "order actually lands" is a risk
the build prompt calls out explicitly (measure hedge/fill latency
empirically); this paper-mode strategy does not model it. Revisit if this
graduates past Phase 1.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from bot.edge import (
    RiskParams,
    annualized_return,
    decide,
    executable_depth,
    kalshi_fee,
    polymarket_fee,
    risk_adjusted_edge,
)
from bot.persistence_tracking import update_divergence_period
from bot.pricing import (
    kalshi_no_ask_depth,
    kalshi_yes_ask_depth,
    polymarket_no_ask_depth,
    polymarket_yes_ask_depth,
)
from bot.state import MarketState

STRATEGY_ID = "locked_pair_arb"

# direction -> (leg_a_venue, leg_b_venue)
DIRECTIONS = {
    "buy_kalshi_yes_poly_no": ("kalshi", "polymarket"),
    "buy_poly_yes_kalshi_no": ("polymarket", "kalshi"),
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _days_to(iso_date: str | None, now: dt.datetime) -> Decimal | None:
    if not iso_date:
        return None
    try:
        target = dt.datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.timezone.utc)
    return Decimal(str(max((target - now).total_seconds() / 86400, 0.01)))


@dataclass
class PairEvaluation:
    pair_id: int
    direction: str
    size: int
    vwap_a: Decimal | None
    vwap_b: Decimal | None
    fee_a: Decimal | None
    fee_b: Decimal | None
    gross_edge_per_contract: Decimal | None
    net_edge_per_contract: Decimal | None
    risk_adjustment_per_contract: Decimal | None
    adjusted_edge_per_contract: Decimal | None
    annual_return: Decimal | None
    traded: bool
    reason: str


# Canonical binding_constraint vocabulary (category expansion task §5) —
# bot scan-report groups by these exact values, so every rejection reason
# gets bucketed into one of them rather than reported as free text.
def _canonical_binding_constraint(reason: str, traded: bool) -> str:
    if traded:
        return "passed"
    if reason == "stale_book":
        return "stale_book"
    if reason == "tier_too_high":
        return "tier_too_high"
    if reason.startswith("adjusted_edge"):
        return "below_min_edge"
    if reason.startswith("annual_return"):
        return "below_annual_hurdle"
    if reason in ("no_executable_depth_above_hurdle", "below_min_viable_size") or reason.startswith("size "):
        return "insufficient_depth"
    return reason  # e.g. "pair_already_has_open_position" — a real state outside the 6-value enum


def has_open_pair_position(conn: sqlite3.Connection, pair_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pair_positions WHERE pair_id = ? AND status = 'open'", (pair_id,)
    ).fetchone()
    return row is not None


def open_pair_position(conn: sqlite3.Connection, pair_id: int, ev: PairEvaluation) -> int:
    leg_a_venue, leg_b_venue = DIRECTIONS[ev.direction]
    entry_cost = ev.size * (ev.vwap_a + ev.vwap_b) + ev.fee_a + ev.fee_b
    now = _now()
    cur = conn.execute(
        "INSERT INTO pair_positions (strategy_id, pair_id, direction, size, leg_a_venue, leg_a_fill_price, "
        "leg_a_fee, leg_b_venue, leg_b_fill_price, leg_b_fee, entry_cost_usd, predicted_edge_per_contract, "
        "predicted_annual_return, status, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (
            STRATEGY_ID, pair_id, ev.direction, ev.size, leg_a_venue, float(ev.vwap_a), float(ev.fee_a),
            leg_b_venue, float(ev.vwap_b), float(ev.fee_b), float(entry_cost),
            float(ev.adjusted_edge_per_contract), float(ev.annual_return), now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _log_evaluation(conn: sqlite3.Connection, ev: PairEvaluation, now: str) -> None:
    def f(x: Decimal | None) -> float | None:
        return float(x) if x is not None else None

    conn.execute(
        "INSERT INTO pair_evaluations (pair_id, ts, direction, gross_edge_per_contract, fee_cost_per_contract, "
        "net_edge_per_contract, risk_adjustment_per_contract, adjusted_edge_per_contract, executable_size, "
        "vwap_a, vwap_b, annualized_return, traded, binding_constraint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ev.pair_id, now, ev.direction, f(ev.gross_edge_per_contract),
            f(ev.fee_a + ev.fee_b if ev.fee_a is not None and ev.fee_b is not None else None),
            f(ev.net_edge_per_contract), f(ev.risk_adjustment_per_contract), f(ev.adjusted_edge_per_contract),
            ev.size, f(ev.vwap_a), f(ev.vwap_b), f(ev.annual_return),
            int(ev.traded), _canonical_binding_constraint(ev.reason, ev.traded),
        ),
    )


class LockedPairArbStrategy:
    strategy_id = STRATEGY_ID

    def __init__(
        self,
        conn: sqlite3.Connection,
        state: MarketState,
        *,
        min_abs_edge: Decimal = Decimal("0.01"),
        hurdle_annual_return: Decimal = Decimal("0.25"),
        min_viable_size: int = 1,
        max_size_per_pair: int = 50,
        notional_usd_cap: Decimal = Decimal("50"),
        settlement_lag_days: Decimal = Decimal("2"),
        max_reviewable_tier: int = 3,
        risk: RiskParams | None = None,
    ) -> None:
        self.conn = conn
        self.state = state
        self.min_abs_edge = min_abs_edge
        self.hurdle_annual_return = hurdle_annual_return
        self.min_viable_size = min_viable_size
        self.max_size_per_pair = max_size_per_pair
        self.notional_usd_cap = notional_usd_cap
        self.settlement_lag_days = settlement_lag_days
        self.max_reviewable_tier = max_reviewable_tier
        self.risk = risk or RiskParams()

    @staticmethod
    def _skipped_evaluation(pair_id: int, reason: str) -> PairEvaluation:
        """Pair-level skip (stale book, tier above the review threshold) —
        logged as a single row, not per-direction, since no book-walking
        happened. Every scan of every verified pair produces at least one
        persisted row, per the task's explicit "regardless of whether it
        passes" requirement — a silent `continue` with nothing logged would
        make the rejection log blind to exactly these cases."""
        return PairEvaluation(pair_id, "n/a", 0, None, None, None, None, None, None, None, None, None, False, reason)

    def _risk_charge_per_contract(self) -> Decimal:
        return (
            self.risk.p_divergence * self.risk.asymmetry
            + self.risk.p_hedge_fail * self.risk.unwind_cost_per_contract
            + self.risk.rebalance_cost_per_contract
        )

    def _evaluate_direction(self, pair_id: int, direction: str, levels_a, levels_b, fee_fn_a, fee_fn_b, t_eff_days: Decimal) -> PairEvaluation:
        risk_charge = self._risk_charge_per_contract()
        result = executable_depth(levels_a, levels_b, fee_fn_a, fee_fn_b, self.min_abs_edge + risk_charge)

        if result.size == 0:
            return PairEvaluation(pair_id, direction, 0, None, None, None, None, None, None, None, None, None, False, "no_executable_depth_above_hurdle")

        size = min(result.size, self.max_size_per_pair)
        binding = "book_depth" if size == result.size else "max_size_per_pair"
        capital_per_contract = result.vwap_a + result.vwap_b + (result.total_fee / result.size)
        if capital_per_contract > 0:
            notional_capped = int(self.notional_usd_cap / capital_per_contract)
            if notional_capped < size:
                size = max(notional_capped, 0)
                binding = "notional_usd_cap"

        # VWAP/fee-per-contract don't change when we merely cap size (we're
        # still buying the same cheapest-first levels, just fewer of them);
        # per-contract economics are unaffected by truncating the tail.
        fee_a = result.total_fee_a * size / result.size
        fee_b = result.total_fee_b * size / result.size
        gross_edge = Decimal(1) - result.vwap_a - result.vwap_b

        if size < self.min_viable_size:
            return PairEvaluation(
                pair_id, direction, size, result.vwap_a, result.vwap_b, fee_a, fee_b, gross_edge,
                result.net_edge_per_contract, None, None, None, False, "below_min_viable_size",
            )

        adjusted = risk_adjusted_edge(result.net_edge_per_contract, self.risk)
        annual = annualized_return(adjusted, capital_per_contract, t_eff_days)
        decision = decide(
            adjusted, annual, size,
            min_abs_edge=self.min_abs_edge, hurdle_annual_return=self.hurdle_annual_return,
            min_viable_size=self.min_viable_size,
        )
        reason = binding if decision.should_trade else decision.reason
        return PairEvaluation(
            pair_id, direction, size, result.vwap_a, result.vwap_b, fee_a, fee_b, gross_edge,
            result.net_edge_per_contract, result.net_edge_per_contract - adjusted, adjusted, annual,
            decision.should_trade, reason,
        )

    async def scan(self) -> list[PairEvaluation]:
        now = _now()
        now_dt = dt.datetime.now(dt.timezone.utc)
        evaluations: list[PairEvaluation] = []

        # Verified pairs (Tier A, trading-eligible) plus unverified Tier B
        # candidates (reviewable divergence tier, not yet human-approved) —
        # the latter are evaluated and logged for the research question the
        # whole category-expansion task exists to answer (does divergence
        # persist outside sports?) but can NEVER open a position; see the
        # `verified` gate right before open_pair_position below.
        pairs = self.conn.execute(
            "SELECT id, polymarket_slug, kalshi_ticker, polymarket_end_date, kalshi_close_date, tier, "
            "category, verified FROM pairs WHERE verified = 1 OR polling_tier = 'B'"
        ).fetchall()

        for pair_id, pm_slug, k_ticker, pm_end, k_close, tier, category, verified in pairs:
            if tier is not None and tier > self.max_reviewable_tier:
                ev = self._skipped_evaluation(pair_id, "tier_too_high")
                _log_evaluation(self.conn, ev, now)
                update_divergence_period(self.conn, pair_id=pair_id, category=category, tier=tier or 0, gross_edge=None, now=now)
                evaluations.append(ev)
                continue

            pm_book = self.state.polymarket_book(pm_slug)
            k_book = self.state.kalshi.get(k_ticker)
            book_missing = not pm_book or not k_book
            book_stale = not book_missing and (self.state.polymarket_is_stale(pm_slug) or self.state.kalshi.is_stale(k_ticker))
            if book_missing or book_stale:
                ev = self._skipped_evaluation(pair_id, "stale_book")
                _log_evaluation(self.conn, ev, now)
                update_divergence_period(self.conn, pair_id=pair_id, category=category, tier=tier or 0, gross_edge=None, now=now)
                evaluations.append(ev)
                continue

            days = [d for d in (_days_to(pm_end, now_dt), _days_to(k_close, now_dt)) if d is not None]
            if not days:
                continue
            t_eff = max(days) + self.settlement_lag_days
            series = k_ticker.split("-")[0] if k_ticker else None

            def kfee(price: Decimal, contracts: int, maker: bool = False, _series: str | None = series) -> Decimal:
                return kalshi_fee(price, contracts, maker=maker, series=_series)

            def pfee(price: Decimal, contracts: int, maker: bool = False) -> Decimal:
                return polymarket_fee(price, contracts, maker=maker)

            k_yes, k_no = kalshi_yes_ask_depth(k_book), kalshi_no_ask_depth(k_book)
            pm_yes, pm_no = polymarket_yes_ask_depth(pm_book), polymarket_no_ask_depth(pm_book)

            evals = [
                self._evaluate_direction(pair_id, "buy_kalshi_yes_poly_no", k_yes, pm_no, kfee, pfee, t_eff),
                self._evaluate_direction(pair_id, "buy_poly_yes_kalshi_no", pm_yes, k_no, pfee, kfee, t_eff),
            ]

            already_open = has_open_pair_position(self.conn, pair_id)
            best_gross_edge = None
            for ev in evals:
                if ev.gross_edge_per_contract is not None:
                    best_gross_edge = ev.gross_edge_per_contract if best_gross_edge is None else max(best_gross_edge, ev.gross_edge_per_contract)

                if not verified and ev.traded:
                    # Tier B candidate: score and log for the persistence
                    # dataset, but the verified gate is absolute — decide()
                    # doesn't know about verification status, so this check
                    # can't be skipped just because it agreed to trade.
                    ev.traded, ev.reason = False, "unverified_pair_not_tradeable"
                elif ev.traded and already_open:
                    ev.traded, ev.reason = False, "pair_already_has_open_position"

                _log_evaluation(self.conn, ev, now)
                if verified and ev.traded:
                    open_pair_position(self.conn, pair_id, ev)
                    already_open = True
                evaluations.append(ev)

            update_divergence_period(
                self.conn, pair_id=pair_id, category=category, tier=tier or 0,
                gross_edge=float(best_gross_edge) if best_gross_edge is not None else None, now=now,
            )

        self.conn.commit()
        return evaluations
