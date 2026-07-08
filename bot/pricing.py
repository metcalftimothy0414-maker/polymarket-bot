from __future__ import annotations

import statistics


def polymarket_best_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids = book.get("bids") or []
    offers = book.get("offers") or []
    best_bid = float(bids[0]["px"]["value"]) if bids else None
    best_ask = float(offers[0]["px"]["value"]) if offers else None
    return best_bid, best_ask


def polymarket_mid_price(book: dict) -> float | None:
    bid, ask = polymarket_best_bid_ask(book)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def polymarket_top_levels(book: dict, n: int = 3) -> dict:
    return {
        "bids": [{"price": lvl["px"]["value"], "qty": lvl["qty"]} for lvl in (book.get("bids") or [])[:n]],
        "offers": [{"price": lvl["px"]["value"], "qty": lvl["qty"]} for lvl in (book.get("offers") or [])[:n]],
    }


def polymarket_book_depth_usd(book: dict, side: str) -> float:
    """side: 'bids' or 'offers'. Depth = sum(price * qty) across all visible levels."""
    levels = book.get(side) or []
    return sum(float(lvl["px"]["value"]) * float(lvl["qty"]) for lvl in levels)


def trade_size_usd(trade: dict) -> float:
    """The WS trade feed's quantity is already USD-denominated (currency: "USD"
    on the field itself, confirmed against live prints) — no price multiplication needed."""
    return float(trade["quantity"]["value"])


def rolling_median_trade_size_usd(trade_sizes: list[float]) -> float | None:
    return statistics.median(trade_sizes) if trade_sizes else None


def kalshi_best_yes_bid_ask(orderbook: dict) -> tuple[float | None, float | None]:
    """Kalshi has no separate ask array: best YES ask = 1 - best NO bid
    (binary complementarity — a NO bid at X is economically a YES ask at 1-X).
    """
    yes_levels = orderbook.get("yes_dollars") or []
    no_levels = orderbook.get("no_dollars") or []
    best_yes_bid = float(yes_levels[0][0]) if yes_levels else None
    best_no_bid = float(no_levels[0][0]) if no_levels else None
    best_yes_ask = (1 - best_no_bid) if best_no_bid is not None else None
    return best_yes_bid, best_yes_ask


def kalshi_mid_price(orderbook: dict) -> float | None:
    bid, ask = kalshi_best_yes_bid_ask(orderbook)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def kalshi_top_levels(orderbook: dict, n: int = 3) -> dict:
    return {
        "yes_bids": (orderbook.get("yes_dollars") or [])[:n],
        "no_bids": (orderbook.get("no_dollars") or [])[:n],
    }


def devig_two_way(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Multiplicative de-vig: normalize two raw (with-vig) implied probabilities to sum to 1."""
    total = prob_a + prob_b
    return prob_a / total, prob_b / total


def consensus_devigged_prob_for_team(game: dict, team_name: str, market_key: str = "h2h") -> float | None:
    """Median de-vigged win probability for team_name across all bookmakers in an
    Odds API /odds game object. Median (not mean) so one outlier book can't skew it.
    """
    devigged: list[float] = []
    for bookmaker in game.get("bookmakers", []):
        market = next((m for m in bookmaker.get("markets", []) if m["key"] == market_key), None)
        if not market or len(market.get("outcomes", [])) != 2:
            continue
        outcomes = market["outcomes"]
        target = next((o for o in outcomes if o["name"] == team_name), None)
        other = next((o for o in outcomes if o["name"] != team_name), None)
        if not target or not other or target["price"] <= 0 or other["price"] <= 0:
            continue
        p_target = 1.0 / target["price"]
        p_other = 1.0 / other["price"]
        dv_target, _ = devig_two_way(p_target, p_other)
        devigged.append(dv_target)
    return statistics.median(devigged) if devigged else None
