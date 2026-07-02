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
"""


def connect(db_path: str | Path = "data/bot.db") -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
