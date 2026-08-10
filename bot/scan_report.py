"""`bot scan-report` (§8 of the category expansion task): per-category
breakdown of the whole universe-expansion pipeline — discovery through
matching through scoring through persistence. This, together with §7's
divergence_periods table, is the actual research output: does divergence
exist outside sports, and does it persist longer there?
"""
from __future__ import annotations

import sqlite3
import statistics

MATCH_CATEGORIES = ["sports", "economic_indicator", "politics_elections", "numeric_threshold", "generic"]


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[idx]


def catalog_summary(conn: sqlite3.Connection) -> dict:
    kalshi_by_category = dict(conn.execute(
        "SELECT COALESCE(category, 'unknown'), COUNT(*) FROM kalshi_catalog GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall())
    polymarket_by_category = dict(conn.execute(
        "SELECT COALESCE(category, 'unknown'), COUNT(*) FROM polymarket_catalog GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall())
    return {
        "kalshi_total": sum(kalshi_by_category.values()),
        "kalshi_by_category": kalshi_by_category,
        "polymarket_total": sum(polymarket_by_category.values()),
        "polymarket_by_category": polymarket_by_category,
    }


def category_report(conn: sqlite3.Connection, category: str) -> dict:
    conn.row_factory = sqlite3.Row

    pairs_matched = conn.execute("SELECT COUNT(*) FROM pairs WHERE category = ?", (category,)).fetchone()[0]
    pairs_by_tier = dict(conn.execute(
        "SELECT tier, COUNT(*) FROM pairs WHERE category = ? GROUP BY tier ORDER BY tier", (category,)
    ).fetchall())

    opportunities = conn.execute(
        "SELECT COUNT(*) FROM pair_evaluations pe JOIN pairs p ON pe.pair_id = p.id WHERE p.category = ?",
        (category,),
    ).fetchone()[0]

    gate_rows = conn.execute(
        "SELECT pe.binding_constraint, COUNT(*) FROM pair_evaluations pe JOIN pairs p ON pe.pair_id = p.id "
        "WHERE p.category = ? GROUP BY pe.binding_constraint ORDER BY 2 DESC",
        (category,),
    ).fetchall()
    gate_counts = dict(gate_rows)
    most_common_constraint = gate_rows[0][0] if gate_rows else None

    net_edges = [r[0] for r in conn.execute(
        "SELECT pe.net_edge_per_contract FROM pair_evaluations pe JOIN pairs p ON pe.pair_id = p.id "
        "WHERE p.category = ? AND pe.traded = 1 AND pe.net_edge_per_contract IS NOT NULL",
        (category,),
    ).fetchall()]
    annual_returns = [r[0] for r in conn.execute(
        "SELECT pe.annualized_return FROM pair_evaluations pe JOIN pairs p ON pe.pair_id = p.id "
        "WHERE p.category = ? AND pe.traded = 1 AND pe.annualized_return IS NOT NULL",
        (category,),
    ).fetchall()]

    durations = [r[0] for r in conn.execute(
        "SELECT duration_seconds FROM divergence_periods WHERE category = ? AND duration_seconds IS NOT NULL",
        (category,),
    ).fetchall()]

    return {
        "category": category,
        "pairs_matched": pairs_matched,
        "pairs_by_tier": pairs_by_tier,
        "opportunities_detected": opportunities,
        "gate_counts": gate_counts,
        "most_common_binding_constraint": most_common_constraint,
        "median_net_edge": statistics.median(net_edges) if net_edges else None,
        "median_annual_return": statistics.median(annual_returns) if annual_returns else None,
        "median_persistence_seconds": statistics.median(durations) if durations else None,
        "p90_persistence_seconds": _percentile(durations, 0.90),
        "persistence_samples": len(durations),
    }


def _fmt(value, spec: str = ".4f") -> str:
    return f"{value:{spec}}" if value is not None else "n/a"


def print_scan_report(conn: sqlite3.Connection) -> None:
    catalog = catalog_summary(conn)
    print("=== catalog (raw venue categories) ===")
    print(f"  Kalshi:     {catalog['kalshi_total']} markets — {catalog['kalshi_by_category']}")
    print(f"  Polymarket: {catalog['polymarket_total']} markets — {catalog['polymarket_by_category']}")
    print()

    for category in MATCH_CATEGORIES:
        r = category_report(conn, category)
        print(f"=== {category} ===")
        print(f"  pairs matched:              {r['pairs_matched']}")
        print(f"  pairs by tier:               {r['pairs_by_tier']}")
        print(f"  opportunities detected:      {r['opportunities_detected']}")
        print(f"  gate breakdown:              {r['gate_counts']}")
        print(f"  most common constraint:     {r['most_common_binding_constraint'] or 'n/a'}")
        print(f"  median net edge:             {_fmt(r['median_net_edge'])}")
        print(f"  median annualized return:    {_fmt(r['median_annual_return'], '.2%')}" if r['median_annual_return'] is not None else "  median annualized return:    n/a")
        print(f"  median divergence persistence: {_fmt(r['median_persistence_seconds'], '.1f')}s ({r['persistence_samples']} samples)")
        print(f"  p90 divergence persistence:  {_fmt(r['p90_persistence_seconds'], '.1f')}s")
        print()
