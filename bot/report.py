from __future__ import annotations

import csv
import math
import sqlite3
import statistics
import sys

STRATEGY_IDS = ["kalshi_divergence", "sportsbook_divergence", "sports_momentum"]
MIN_OPPORTUNITIES_FOR_KILL_CHECK = 100
MIN_RESOLVED_FOR_BASE_RATE_TEST = 200
Z_CRITICAL_95 = 1.96


def _median_persistence_seconds(conn: sqlite3.Connection, strategy_id: str) -> float | None:
    rows = [
        r[0] for r in conn.execute(
            "SELECT persistence_seconds FROM opportunities WHERE strategy_id = ? AND persistence_seconds IS NOT NULL",
            (strategy_id,),
        ).fetchall()
    ]
    return statistics.median(rows) if rows else None


def _edge_cents_per_trade(row: sqlite3.Row) -> float | None:
    if row["realized_pnl_usd"] is None or not row["fill_price"] or not row["notional_usd"]:
        return None
    shares = row["notional_usd"] / row["fill_price"]
    return (row["realized_pnl_usd"] / shares) * 100


def max_drawdown(conn: sqlite3.Connection, strategy_id: str) -> float:
    rows = conn.execute(
        "SELECT realized_pnl_usd FROM paper_trades WHERE strategy_id = ? AND status = 'closed' ORDER BY closed_at",
        (strategy_id,),
    ).fetchall()
    cumulative, peak, worst = 0.0, 0.0, 0.0
    for (pnl,) in rows:
        cumulative += pnl or 0.0
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


def base_rate_test(conn: sqlite3.Connection, strategy_id: str = "sports_momentum") -> dict:
    """Strategy C's extra kill criterion: is the realized win rate distinguishable
    from what buying at the average entry price alone would predict? Buying at
    55c and winning 55% of the time is zero edge — the null hypothesis here is
    "win rate == average entry price", not "win rate == 50%". Wald z-test
    (stdlib math only, no scipy needed for a one-sample proportion test)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT realized_pnl_usd, fill_price FROM paper_trades WHERE strategy_id = ? AND exit_reason = 'resolution'",
        (strategy_id,),
    ).fetchall()
    n = len(rows)
    if n < MIN_RESOLVED_FOR_BASE_RATE_TEST:
        return {"n": n, "sufficient": False}

    wins = sum(1 for r in rows if r["realized_pnl_usd"] is not None and r["realized_pnl_usd"] > 0)
    win_rate = wins / n
    avg_entry_price = statistics.mean(r["fill_price"] for r in rows)
    variance = avg_entry_price * (1 - avg_entry_price) / n
    z = (win_rate - avg_entry_price) / math.sqrt(variance) if variance > 0 else 0.0
    distinguishable = abs(z) >= Z_CRITICAL_95
    return {
        "n": n, "sufficient": True, "win_rate": win_rate, "avg_entry_price": avg_entry_price,
        "z_score": z, "distinguishable": distinguishable, "outperforming": distinguishable and win_rate > avg_entry_price,
    }


def strategy_metrics(conn: sqlite3.Connection, strategy_id: str) -> dict:
    conn.row_factory = sqlite3.Row

    opportunities_detected = conn.execute(
        "SELECT COUNT(*) FROM opportunities WHERE strategy_id = ?", (strategy_id,)
    ).fetchone()[0]

    trades = conn.execute("SELECT * FROM paper_trades WHERE strategy_id = ?", (strategy_id,)).fetchall()
    total_attempts = len(trades)
    filled = [t for t in trades if t["status"] != "unfilled"]
    closed = [t for t in trades if t["status"] == "closed"]

    fill_rate = (len(filled) / total_attempts) if total_attempts else None
    win_rate = (sum(1 for t in closed if (t["realized_pnl_usd"] or 0) > 0) / len(closed)) if closed else None
    edges = [e for t in closed if (e := _edge_cents_per_trade(t)) is not None]
    avg_edge_cents = statistics.mean(edges) if edges else None
    total_pnl = sum(t["realized_pnl_usd"] or 0 for t in closed)

    return {
        "strategy_id": strategy_id,
        "opportunities_detected": opportunities_detected,
        "median_persistence_seconds": _median_persistence_seconds(conn, strategy_id),
        "fill_rate": fill_rate,
        "win_rate": win_rate,
        "avg_edge_cents": avg_edge_cents,
        "max_drawdown_usd": max_drawdown(conn, strategy_id),
        "total_pnl_usd": total_pnl,
        "open_positions": total_attempts - len(closed) - sum(1 for t in trades if t["status"] == "unfilled"),
    }


def evaluate_kill_criteria(metrics: dict, base_rate: dict | None = None) -> tuple[bool, list[str]]:
    if metrics["opportunities_detected"] < MIN_OPPORTUNITIES_FOR_KILL_CHECK:
        return False, [f"insufficient data ({metrics['opportunities_detected']}/{MIN_OPPORTUNITIES_FOR_KILL_CHECK}+ opportunities needed)"]

    reasons = []
    if metrics["fill_rate"] is not None and metrics["fill_rate"] < 0.30:
        reasons.append(f"fill rate {metrics['fill_rate']:.0%} < 30%")
    if metrics["avg_edge_cents"] is not None and metrics["avg_edge_cents"] < 1.0:
        reasons.append(f"avg edge {metrics['avg_edge_cents']:.2f}c < 1.0c")
    if metrics["median_persistence_seconds"] is not None and metrics["median_persistence_seconds"] < 10:
        reasons.append(f"median persistence {metrics['median_persistence_seconds']:.1f}s < 10s")
    if base_rate and base_rate.get("sufficient") and not base_rate["distinguishable"]:
        reasons.append(
            f"win rate {base_rate['win_rate']:.1%} not distinguishable from avg entry price "
            f"{base_rate['avg_entry_price']:.1%} (z={base_rate['z_score']:.2f}) after {base_rate['n']}+ resolved trades"
        )
    return (len(reasons) > 0), reasons


def _fmt(value, spec: str = ".2f", suffix: str = "") -> str:
    return f"{value:{spec}}{suffix}" if value is not None else "n/a"


def print_report(conn: sqlite3.Connection) -> None:
    all_metrics = {sid: strategy_metrics(conn, sid) for sid in STRATEGY_IDS}
    base_rate = base_rate_test(conn, "sports_momentum")

    for sid in STRATEGY_IDS:
        m = all_metrics[sid]
        is_dead, reasons = evaluate_kill_criteria(m, base_rate if sid == "sports_momentum" else None)
        print(f"=== {sid}{' [DEAD]' if is_dead else ''} ===")
        print(f"  opportunities detected:     {m['opportunities_detected']}")
        print(f"  median persistence:         {_fmt(m['median_persistence_seconds'], '.1f', 's')}")
        print(f"  simulated fill rate:        {_fmt(m['fill_rate'], '.1%')}")
        print(f"  win rate:                   {_fmt(m['win_rate'], '.1%')}")
        print(f"  avg realized edge (fees in): {_fmt(m['avg_edge_cents'], '.2f', 'c')}")
        print(f"  max drawdown:               ${_fmt(m['max_drawdown_usd'])}")
        print(f"  total simulated P&L:        ${_fmt(m['total_pnl_usd'])}")
        print(f"  open positions:             {m['open_positions']}")
        if sid == "sports_momentum" and base_rate.get("sufficient"):
            print(f"  base-rate test: win_rate={base_rate['win_rate']:.1%} vs avg_entry={base_rate['avg_entry_price']:.1%} "
                  f"z={base_rate['z_score']:.2f} distinguishable={base_rate['distinguishable']}")
        if reasons:
            for r in reasons:
                print(f"    -> {r}")
        print()

    print("=== side-by-side comparison ===")
    header = f"{'metric':<28}" + "".join(f"{sid:>22}" for sid in STRATEGY_IDS)
    print(header)
    rows = [
        ("opportunities detected", lambda m: str(m["opportunities_detected"])),
        ("median persistence (s)", lambda m: _fmt(m["median_persistence_seconds"], ".1f")),
        ("fill rate", lambda m: _fmt(m["fill_rate"], ".1%")),
        ("win rate", lambda m: _fmt(m["win_rate"], ".1%")),
        ("avg edge (c)", lambda m: _fmt(m["avg_edge_cents"], ".2f")),
        ("max drawdown ($)", lambda m: _fmt(m["max_drawdown_usd"])),
        ("total P&L ($)", lambda m: _fmt(m["total_pnl_usd"])),
    ]
    for label, fn in rows:
        print(f"{label:<28}" + "".join(f"{fn(all_metrics[sid]):>22}" for sid in STRATEGY_IDS))


def export_csv(conn: sqlite3.Connection, out_path: str) -> int:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM paper_trades ORDER BY opened_at").fetchall()
    if not rows:
        print("No paper trades to export.", file=sys.stderr)
        return 0
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return len(rows)
