from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Protocol


def hash_params(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]


def find_open_opportunity(conn: sqlite3.Connection, strategy_id: str, pair_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM opportunities WHERE pair_id = ? AND strategy_id = ? AND status = 'open'",
        (pair_id, strategy_id),
    ).fetchone()


def insert_opportunity(
    conn: sqlite3.Connection, strategy_id: str, params_hash: str, pair_id: int, market_ref: str,
    direction: str, signal_value: float, entry_price: float, top_levels: dict, now: str,
    extra: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO opportunities "
        "(strategy_id, params_hash, pair_id, market_ref, direction, signal_value, entry_price, "
        "top_levels_json, extra_json, detected_at, last_seen_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
        (strategy_id, params_hash, pair_id, market_ref, direction, signal_value,
         entry_price, json.dumps(top_levels), json.dumps(extra or {}), now, now),
    )


def touch_opportunity(conn: sqlite3.Connection, opp_id: int, now: str) -> None:
    conn.execute("UPDATE opportunities SET last_seen_at = ? WHERE id = ?", (now, opp_id))


def close_opportunity(conn: sqlite3.Connection, opp: sqlite3.Row, now: str) -> None:
    detected = dt.datetime.fromisoformat(opp["detected_at"])
    closed = dt.datetime.fromisoformat(now)
    persistence = (closed - detected).total_seconds()
    conn.execute(
        "UPDATE opportunities SET status = 'closed', closed_at = ?, persistence_seconds = ? WHERE id = ?",
        (now, persistence, opp["id"]),
    )


@dataclass
class Opportunity:
    strategy_id: str
    params_hash: str
    detected_at: str
    market_ref: str
    direction: str
    signal_value: float
    entry_price: float
    top_levels_json: str
    extra_json: str = "{}"


class Strategy(Protocol):
    strategy_id: str

    def params_hash(self) -> str: ...

    async def scan(self) -> list[Opportunity]: ...
