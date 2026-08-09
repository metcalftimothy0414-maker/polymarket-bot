"""Builds a single JSON snapshot of everything the public dashboard shows.

Read-only: queries the same SQLite DB the running bot writes to. Meant to
be called on a short interval (see scripts/publish_dashboard.sh) and its
output committed to the gh-pages branch — the static site just fetches
this file and re-renders, no server involved.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from bot.report import STRATEGY_IDS, base_rate_test, evaluate_kill_criteria, locked_pair_arb_metrics, strategy_metrics
from bot.runner import HEARTBEAT_INTERVAL_SECONDS

# runner.py heartbeats every HEARTBEAT_INTERVAL_SECONDS (300s) — a threshold
# tighter than that would flag a perfectly healthy bot as stale in the gap
# between heartbeats. 2x gives one missed beat of slack before alarming.
STALE_FEED_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 2


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _seconds_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()


def _feed_health(conn: sqlite3.Connection) -> dict:
    last_heartbeat = conn.execute("SELECT ts FROM heartbeats ORDER BY ts DESC LIMIT 1").fetchone()
    last_heartbeat_ts = last_heartbeat[0] if last_heartbeat else None
    age = _seconds_since(last_heartbeat_ts)
    return {
        "last_heartbeat_at": last_heartbeat_ts,
        "heartbeat_age_seconds": age,
        "runner_alive": age is not None and age < STALE_FEED_SECONDS,
    }


def _recent_errors(conn: sqlite3.Connection, limit: int = 10, max_age_hours: float = 24) -> list[dict]:
    """Only truly recent errors — an old error sitting under a "recent" heading
    reads as an ongoing problem when it's actually long resolved/irrelevant."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT ts, component, message FROM errors ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    cutoff_seconds = max_age_hours * 3600
    return [dict(r) for r in rows if (age := _seconds_since(r["ts"])) is not None and age <= cutoff_seconds]


def _pair_counts(conn: sqlite3.Connection) -> dict:
    return {
        "kalshi_pairs_total": conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0],
        "kalshi_pairs_verified": conn.execute("SELECT COUNT(*) FROM pairs WHERE verified = 1").fetchone()[0],
        "odds_pairs_total": conn.execute("SELECT COUNT(*) FROM odds_pairs").fetchone()[0],
        "odds_pairs_verified": conn.execute("SELECT COUNT(*) FROM odds_pairs WHERE verified = 1").fetchone()[0],
    }


def _recent_opportunities(conn: sqlite3.Connection, limit: int = 25) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT strategy_id, market_ref, direction, signal_value, entry_price, detected_at, status "
        "FROM opportunities ORDER BY detected_at DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _recent_pair_evaluations(conn: sqlite3.Connection, limit: int = 25) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT pair_id, ts, direction, adjusted_edge_per_contract, executable_size, annualized_return, "
        "traded, binding_constraint FROM pair_evaluations ORDER BY ts DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _recent_trades(conn: sqlite3.Connection, limit: int = 25) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT strategy_id, market_ref, direction, fill_price, notional_usd, status, realized_pnl_usd, "
        "opened_at, closed_at FROM paper_trades ORDER BY opened_at DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _recent_pair_positions(conn: sqlite3.Connection, limit: int = 25) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT pair_id, direction, size, leg_a_venue, leg_a_fill_price, leg_b_venue, leg_b_fill_price, "
        "entry_cost_usd, predicted_edge_per_contract, status, opened_at, closed_at "
        "FROM pair_positions ORDER BY opened_at DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def build_snapshot(conn: sqlite3.Connection) -> dict:
    strategies = {}
    for sid in STRATEGY_IDS:
        m = strategy_metrics(conn, sid)
        br = base_rate_test(conn, sid) if sid == "sportsbook_divergence" else None
        is_dead, reasons = evaluate_kill_criteria(m, br)
        strategies[sid] = {**m, "base_rate_test": br, "dead": is_dead, "kill_reasons": reasons}

    return {
        "generated_at": _now_iso(),
        "mode": "PAPER",
        "feed_health": _feed_health(conn),
        "pair_counts": _pair_counts(conn),
        "strategies": strategies,
        "locked_pair_arb": locked_pair_arb_metrics(conn),
        "markets_discovered": conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
        "recent_opportunities": _recent_opportunities(conn),
        "recent_pair_evaluations": _recent_pair_evaluations(conn),
        "recent_trades": _recent_trades(conn),
        "recent_pair_positions": _recent_pair_positions(conn),
        "recent_errors": _recent_errors(conn),
    }
