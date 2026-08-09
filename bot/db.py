from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    slug TEXT PRIMARY KEY,
    question TEXT,
    category TEXT,
    end_date TEXT,
    game_start_time TEXT,
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
    category TEXT NOT NULL DEFAULT 'sports',
    tier INTEGER,
    tier_reasons TEXT,
    polling_tier TEXT NOT NULL DEFAULT 'C',
    UNIQUE(polymarket_slug, kalshi_ticker)
);

CREATE TABLE IF NOT EXISTS odds_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polymarket_slug TEXT NOT NULL,
    odds_api_game_id TEXT NOT NULL,
    odds_api_sport_key TEXT NOT NULL,
    long_team TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    polymarket_question TEXT,
    polymarket_description TEXT,
    polymarket_end_date TEXT,
    odds_api_matchup TEXT,
    odds_api_commence_time TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(polymarket_slug, odds_api_game_id)
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
    status TEXT NOT NULL DEFAULT 'open',
    counterfactual_60s REAL,
    counterfactual_300s REAL
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    market_ref TEXT NOT NULL,
    ts TEXT NOT NULL,
    implied_prob REAL,
    momentum_value REAL,
    spread_cents REAL,
    depth_usd REAL,
    seconds_remaining REAL,
    traded INTEGER NOT NULL DEFAULT 0,
    trade_size_usd REAL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    opportunity_id INTEGER,
    market_ref TEXT NOT NULL,
    direction TEXT NOT NULL,
    signal_entry_price REAL NOT NULL,
    fill_price REAL,
    notional_usd REAL NOT NULL,
    entry_fee REAL,
    exit_price REAL,
    exit_fee REAL,
    exit_reason TEXT,
    realized_pnl_usd REAL,
    status TEXT NOT NULL DEFAULT 'pending_fill',
    opened_at TEXT NOT NULL,
    filled_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    strategy_id TEXT NOT NULL,
    pair_key TEXT NOT NULL,
    label TEXT,
    polymarket_price REAL,
    other_venue_price REAL,
    divergence_cents REAL,
    entry_threshold_cents REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (strategy_id, pair_key)
);

-- Every locked_pair_arb evaluation, accepted or not — per the build prompt's
-- §7.4: the rejection log is the research dataset that tells you whether the
-- strategy is capacity-, fee-, or risk-constrained.
CREATE TABLE IF NOT EXISTS pair_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    direction TEXT NOT NULL,
    gross_edge_per_contract REAL,
    fee_cost_per_contract REAL,
    net_edge_per_contract REAL,
    risk_adjustment_per_contract REAL,
    adjusted_edge_per_contract REAL,
    executable_size INTEGER,
    vwap_a REAL,
    vwap_b REAL,
    annualized_return REAL,
    traded INTEGER NOT NULL DEFAULT 0,
    binding_constraint TEXT
);

CREATE TABLE IF NOT EXISTS pair_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    pair_id INTEGER NOT NULL,
    evaluation_id INTEGER,
    direction TEXT NOT NULL,
    size INTEGER NOT NULL,
    leg_a_venue TEXT NOT NULL,
    leg_a_fill_price REAL NOT NULL,
    leg_a_fee REAL NOT NULL,
    leg_b_venue TEXT NOT NULL,
    leg_b_fill_price REAL NOT NULL,
    leg_b_fee REAL NOT NULL,
    entry_cost_usd REAL NOT NULL,
    predicted_edge_per_contract REAL,
    predicted_annual_return REAL,
    status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_position_id INTEGER NOT NULL,
    pair_id INTEGER NOT NULL,
    kalshi_outcome REAL,
    polymarket_outcome REAL,
    diverged INTEGER NOT NULL,
    gross_payout_usd REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL,
    predicted_edge_per_contract REAL,
    edge_error_usd REAL,
    settled_at TEXT NOT NULL
);

-- Per build prompt (category expansion task) §1: the published formula's
-- own default is M_taker=1, M_maker=0; sports series override M_maker to 1;
-- a short explicit list is 0/0. Populated by bot.fee_multipliers, refreshed
-- daily. `source` distinguishes a confirmed override from the raw default,
-- so a caller can tell "we checked and it's 1/0" from "we haven't checked".
CREATE TABLE IF NOT EXISTS kalshi_series_multipliers (
    series_ticker TEXT PRIMARY KEY,
    m_taker REAL NOT NULL,
    m_maker REAL NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- Full-catalog discovery (§2), separate from the existing `markets` table
-- (which is Polymarket-only and tied to the sports watchlist path) so
-- universe-wide enumeration can't regress sports behavior.
CREATE TABLE IF NOT EXISTS kalshi_catalog (
    ticker TEXT PRIMARY KEY,
    series_ticker TEXT NOT NULL,
    event_ticker TEXT,
    title TEXT,
    category TEXT,
    rules_primary TEXT,
    rules_secondary TEXT,
    close_time TEXT,
    expiration_time TEXT,
    occurrence_datetime TEXT,
    status TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polymarket_catalog (
    slug TEXT PRIMARY KEY,
    question TEXT,
    category TEXT,
    market_type TEXT,
    description TEXT,
    tick_size REAL,
    neg_risk INTEGER,
    game_start_time TEXT,
    end_date TEXT,
    status TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

-- Divergence-persistence measurement (§7) — the research output the whole
-- category-expansion task exists to produce: does divergence exist outside
-- sports, and does it last longer there? One row per open-to-close cycle.
CREATE TABLE IF NOT EXISTS divergence_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    tier INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    peak_edge REAL NOT NULL,
    duration_seconds REAL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS is a no-op on tables that already exist, so a
    new column needs an explicit, idempotent ALTER for databases created before it."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    if "trade_size_usd" not in columns:
        conn.execute("ALTER TABLE candidates ADD COLUMN trade_size_usd REAL")

    # Category expansion (§3/§4/§6): every existing sports pair defaults to
    # category='sports' so old rows stay queryable/filterable exactly like
    # new ones, and polling_tier='A' so already-verified pairs don't drop to
    # a slower cadence on upgrade.
    pairs_columns = {row[1] for row in conn.execute("PRAGMA table_info(pairs)")}
    if "category" not in pairs_columns:
        conn.execute("ALTER TABLE pairs ADD COLUMN category TEXT NOT NULL DEFAULT 'sports'")
    if "tier" not in pairs_columns:
        conn.execute("ALTER TABLE pairs ADD COLUMN tier INTEGER")
    if "tier_reasons" not in pairs_columns:
        conn.execute("ALTER TABLE pairs ADD COLUMN tier_reasons TEXT")
    if "polling_tier" not in pairs_columns:
        conn.execute("ALTER TABLE pairs ADD COLUMN polling_tier TEXT NOT NULL DEFAULT 'C'")


def connect(db_path: str | Path = "data/bot.db") -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn
