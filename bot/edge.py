"""Fee-and-risk-adjusted edge math for cross-venue locked-pair arbitrage
(Kalshi + Polymarket US), per docs/DECISIONS.md and docs/VERIFIED_FACTS.md.

All money quantities are Decimal — never float — because the fee curve's
rounding rules (Kalshi: roundup to a centicent; Polymarket US: banker's
rounding to a cent) are only correct with exact decimal arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_UP, Decimal

CENTICENT = Decimal("0.0001")
CENT = Decimal("0.01")
ONE = Decimal(1)

KALSHI_TAKER_THETA = Decimal("0.07")
KALSHI_MAKER_THETA = Decimal("0.0175")
POLYMARKET_TAKER_THETA = Decimal("0.06")
POLYMARKET_MAKER_THETA = Decimal("0.0125")

# Non-standard per-series Kalshi multiplier table. The published formula's
# own default is M_taker=1, M_maker=0 — but most real series (sports,
# Fed/CPI/GDP, awards) override M_maker to 1 in Kalshi's Non-Standard Fees
# table, which VERIFIED_FACTS.md could not fetch (PDF fetch failed in this
# environment: TLS cert error). This starts empty; an operator must populate
# confirmed series here before a maker-side EV calculation on an unlisted
# series can be trusted. `kalshi_series_multipliers` returns whether the
# series was found so callers (e.g. the strategy layer) can refuse to size a
# maker leg on an unconfirmed series rather than silently trusting M=0.
KALSHI_SERIES_MULTIPLIERS: dict[str, tuple[Decimal, Decimal]] = {
    # "KXBTCY": (Decimal(0), Decimal(0)),  # example of a confirmed 0/0 series
}
DEFAULT_KALSHI_MAKER_MULTIPLIER = Decimal(0)
DEFAULT_KALSHI_TAKER_MULTIPLIER = Decimal(1)


def kalshi_series_multipliers(series: str | None) -> tuple[Decimal, Decimal, bool]:
    """Returns (M_taker, M_maker, confirmed) for a Kalshi series ticker."""
    if series and series in KALSHI_SERIES_MULTIPLIERS:
        m_taker, m_maker = KALSHI_SERIES_MULTIPLIERS[series]
        return m_taker, m_maker, True
    return DEFAULT_KALSHI_TAKER_MULTIPLIER, DEFAULT_KALSHI_MAKER_MULTIPLIER, False


def kalshi_fee(price: Decimal, contracts: int, *, maker: bool, series: str | None = None) -> Decimal:
    """Fee in dollars. Positive = cost. Kalshi has no maker rebate (M_maker=1
    is a cost, not a credit — unlike Polymarket US; M_maker=0 series are
    free, not a rebate)."""
    m_taker, m_maker, _confirmed = kalshi_series_multipliers(series)
    theta = KALSHI_MAKER_THETA if maker else KALSHI_TAKER_THETA
    m = m_maker if maker else m_taker
    raw = m * theta * Decimal(contracts) * price * (ONE - price)
    return raw.quantize(CENTICENT, rounding=ROUND_UP)


def polymarket_fee(price: Decimal, contracts: int, *, maker: bool) -> Decimal:
    """Fee in dollars. Positive = cost, negative = maker rebate (credit)."""
    theta = POLYMARKET_MAKER_THETA if maker else POLYMARKET_TAKER_THETA
    raw = theta * Decimal(contracts) * price * (ONE - price)
    rounded = raw.quantize(CENT, rounding=ROUND_HALF_EVEN)
    return -rounded if maker else rounded


def polymarket_multi_fill_taker_fee(fills: list[tuple[Decimal, int]]) -> Decimal:
    """Total taker commission across an order that swept multiple price
    levels. Per docs.polymarket.us/fees: each fill is banker's-rounded
    independently, then the total is capped at the banker's-rounded
    cumulative exact fee (adjustment can only reduce, never increase).

    ponytail: caps the *total* rather than reproducing which specific fill
    absorbs the reduction — we only need total commission for EV, not a
    per-fill order-book reconciliation. Revisit if per-fill fee attribution
    is ever needed (e.g. matching a venue statement line by line).
    """
    if not fills:
        return Decimal(0)
    exact_total = sum(
        (POLYMARKET_TAKER_THETA * Decimal(c) * p * (ONE - p) for p, c in fills), Decimal(0)
    )
    cumulative_cap = exact_total.quantize(CENT, rounding=ROUND_HALF_EVEN)
    per_fill_total = sum(
        ((POLYMARKET_TAKER_THETA * Decimal(c) * p * (ONE - p)).quantize(CENT, rounding=ROUND_HALF_EVEN) for p, c in fills),
        Decimal(0),
    )
    return min(per_fill_total, cumulative_cap)


@dataclass
class BookLevel:
    price: Decimal
    size: int


@dataclass
class WalkResult:
    size: int
    vwap_a: Decimal | None
    vwap_b: Decimal | None
    total_fee: Decimal
    net_edge_total: Decimal
    net_edge_per_contract: Decimal | None
    total_fee_a: Decimal = Decimal(0)
    total_fee_b: Decimal = Decimal(0)


def executable_depth(
    levels_a: list[BookLevel],
    levels_b: list[BookLevel],
    fee_fn_a,
    fee_fn_b,
    min_edge_per_contract: Decimal,
) -> WalkResult:
    """Walk two ask-side depth ladders (cost to acquire one more unit of the
    'buy YES on A' leg and 'buy NO on B' leg, cheapest first on each side) in
    lockstep, stopping at the first contract whose marginal net edge falls
    below min_edge_per_contract. Returns the marginal-optimal size, not the
    largest size with positive *average* edge — those differ and the
    difference is real money (build prompt §5.2).
    """
    ia = ib = 0
    used_a = used_b = 0
    size = 0
    cost_a = cost_b = Decimal(0)
    fee_a_total = fee_b_total = Decimal(0)

    while ia < len(levels_a) and ib < len(levels_b):
        la, lb = levels_a[ia], levels_b[ib]
        fee_a = fee_fn_a(la.price, 1)
        fee_b = fee_fn_b(lb.price, 1)
        marginal_cost = la.price + lb.price + fee_a + fee_b
        marginal_edge = ONE - marginal_cost
        if marginal_edge < min_edge_per_contract:
            break
        size += 1
        cost_a += la.price
        cost_b += lb.price
        fee_a_total += fee_a
        fee_b_total += fee_b
        used_a += 1
        used_b += 1
        if used_a >= la.size:
            ia += 1
            used_a = 0
        if used_b >= lb.size:
            ib += 1
            used_b = 0

    if size == 0:
        return WalkResult(0, None, None, Decimal(0), Decimal(0), None)

    total_fee = fee_a_total + fee_b_total
    net_edge_total = Decimal(size) - cost_a - cost_b - total_fee
    return WalkResult(
        size, cost_a / size, cost_b / size, total_fee, net_edge_total, net_edge_total / size,
        total_fee_a=fee_a_total, total_fee_b=fee_b_total,
    )


@dataclass
class RiskParams:
    """Per build prompt §5.3. Defaults are deliberately pessimistic starting
    priors, not calibrated estimates — calibrate P(divergence) per category
    from settlement history (bot.strategies.locked_pair_arb records the
    inputs needed for that in the `settlements` table)."""

    p_divergence: Decimal = Decimal("0.02")
    asymmetry: Decimal = Decimal("0.85")
    p_hedge_fail: Decimal = Decimal("0")
    unwind_cost_per_contract: Decimal = Decimal("0")
    rebalance_cost_per_contract: Decimal = Decimal("0")


def risk_adjusted_edge(net_edge_per_contract: Decimal, risk: RiskParams) -> Decimal:
    divergence_charge = risk.p_divergence * risk.asymmetry * ONE
    hedge_fail_charge = risk.p_hedge_fail * risk.unwind_cost_per_contract
    return net_edge_per_contract - divergence_charge - hedge_fail_charge - risk.rebalance_cost_per_contract


def annualized_return(adjusted_edge_per_contract: Decimal, capital_per_contract: Decimal, t_eff_days: Decimal) -> Decimal:
    """(1 + edge/capital)^(365/days) - 1. Decimal doesn't support fractional
    exponents; the compounding step goes through float (fine — this is a
    reporting/decision number, not a money amount that gets persisted or
    summed)."""
    if capital_per_contract <= 0 or t_eff_days <= 0:
        return Decimal(0)
    r_simple = adjusted_edge_per_contract / capital_per_contract
    compounded = float(ONE + r_simple) ** (365.0 / float(t_eff_days))
    return Decimal(str(compounded - 1))


@dataclass
class Decision:
    should_trade: bool
    reason: str


def decide(
    adjusted_edge_per_contract: Decimal,
    annual_return: Decimal,
    size: int,
    *,
    min_abs_edge: Decimal = Decimal("0.01"),
    hurdle_annual_return: Decimal = Decimal("0.25"),
    min_viable_size: int = 1,
) -> Decision:
    if adjusted_edge_per_contract < min_abs_edge:
        return Decision(False, f"adjusted_edge {adjusted_edge_per_contract} < min_abs_edge {min_abs_edge}")
    if annual_return < hurdle_annual_return:
        return Decision(False, f"annual_return {annual_return} < hurdle {hurdle_annual_return}")
    if size < min_viable_size:
        return Decision(False, f"size {size} < min_viable_size {min_viable_size}")
    return Decision(True, "ok")
