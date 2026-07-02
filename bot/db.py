from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    slug TEXT PRIMARY KEY,
    question TEXT,
    category TEXT,
    end_date TEXT,
    active INTEGER,
    closed INTEGER,
    raw_json TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    component TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polymarket_slug TEXT NOT NULL,
    kalshi_ticker TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    polymarket_question TEXT,
    polymarket_description TEXT,
    polymarket_end_date TEXT,
    kalshi_title TEXT,
    kalshi_rules TEXT,
    kalshi_close_date TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(polymarket_slug, kalshi_ticker)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    pair_id INTEGER,
    market_ref TEXT NOT NULL,
    direction TEXT NOT NULL,
    signal_value REAL NOT NULL,
    entry_price REAL NOT NULL,
    top_levels_json TEXT,
    extra_json TEXT,
    detected_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    closed_at TEXT,
    persistence_seconds REAL,
    status TEXT NOT NULL DEFAULT 'open'
);
"""


def connect(db_path: str | Path = "data/bot.db") -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
