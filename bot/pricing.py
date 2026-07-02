from __future__ import annotations


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
