from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from bot import db

STATIC_DIR = Path(__file__).parent / "static"
POLL_INTERVAL_SECONDS = 1.5
STRATEGY_IDS = ["kalshi_divergence", "sportsbook_divergence", "sports_momentum", "large_flow"]
MAX_TRADE_LOG_ROWS = 60
MAX_EQUITY_POINTS = 500

app = FastAPI()
_connections: set[WebSocket] = set()
_start_time = time.monotonic()


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def build_state(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    today = _today()

    closed_trades = conn.execute("SELECT * FROM paper_trades WHERE status = 'closed'").fetchall()
    total_pnl = sum(t["realized_pnl_usd"] or 0 for t in closed_trades)
    win_rate = (
        sum(1 for t in closed_trades if (t["realized_pnl_usd"] or 0) > 0) / len(closed_trades)
        if closed_trades else None
    )
    open_positions = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status IN ('open', 'pending_fill')"
    ).fetchone()[0]
    opportunities_today = conn.execute(
        "SELECT COUNT(*) FROM opportunities WHERE detected_at >= ?", (today,)
    ).fetchone()[0]

    per_strategy_pnl = {}
    equity_curves: dict[str, list[list[float]]] = {"ALL": []}
    for sid in STRATEGY_IDS:
        rows = conn.execute(
            "SELECT realized_pnl_usd, closed_at FROM paper_trades "
            "WHERE strategy_id = ? AND status = 'closed' ORDER BY closed_at", (sid,),
        ).fetchall()
        per_strategy_pnl[sid] = sum(r["realized_pnl_usd"] or 0 for r in rows)
        cumulative = 0.0
        curve = []
        for r in rows[-MAX_EQUITY_POINTS:]:
            cumulative += r["realized_pnl_usd"] or 0
            curve.append([r["closed_at"], round(cumulative, 4)])
        equity_curves[sid] = curve

    all_rows = conn.execute(
        "SELECT realized_pnl_usd, closed_at FROM paper_trades WHERE status = 'closed' ORDER BY closed_at"
    ).fetchall()
    cumulative = 0.0
    for r in all_rows[-MAX_EQUITY_POINTS:]:
        cumulative += r["realized_pnl_usd"] or 0
        equity_curves["ALL"].append([r["closed_at"], round(cumulative, 4)])

    markets = [dict(r) for r in conn.execute(
        "SELECT * FROM market_snapshots ORDER BY updated_at DESC"
    ).fetchall()]

    trade_rows = [dict(r) for r in conn.execute(
        "SELECT id, strategy_id, market_ref, direction, fill_price, exit_price, realized_pnl_usd, "
        "status, exit_reason, opened_at, closed_at FROM paper_trades ORDER BY opened_at DESC LIMIT ?",
        (MAX_TRADE_LOG_ROWS,),
    ).fetchall()]
    opp_rows = [dict(r) for r in conn.execute(
        "SELECT id, strategy_id, market_ref, direction, signal_value, entry_price, detected_at, status "
        "FROM opportunities ORDER BY detected_at DESC LIMIT ?", (MAX_TRADE_LOG_ROWS,),
    ).fetchall()]

    last_heartbeat = conn.execute("SELECT ts FROM heartbeats ORDER BY ts DESC LIMIT 1").fetchone()
    error_count_today = conn.execute("SELECT COUNT(*) FROM errors WHERE ts >= ?", (today,)).fetchone()[0]

    return {
        "mode": "PAPER",
        "stats": {
            "total_pnl": round(total_pnl, 2),
            "win_rate": win_rate,
            "open_positions": open_positions,
            "opportunities_today": opportunities_today,
            "per_strategy_pnl": {k: round(v, 2) for k, v in per_strategy_pnl.items()},
        },
        "markets": markets,
        "equity_curves": equity_curves,
        "trades": trade_rows,
        "opportunities": opp_rows,
        "footer": {
            "last_heartbeat": last_heartbeat["ts"] if last_heartbeat else None,
            "server_uptime_seconds": round(time.monotonic() - _start_time),
            "error_count_today": error_count_today,
        },
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_DIR / "index.html").read_text()


@app.websocket("/ws/ui")
async def ws_ui(websocket: WebSocket) -> None:
    await websocket.accept()
    _connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # client sends nothing meaningful; just detect disconnect
    except WebSocketDisconnect:
        pass
    finally:
        _connections.discard(websocket)


async def _broadcast_loop() -> None:
    conn = db.connect()
    while True:
        try:
            state = build_state(conn)
            payload = json.dumps(state)
            dead = []
            for ws in _connections:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _connections.discard(ws)
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(_broadcast_loop())
