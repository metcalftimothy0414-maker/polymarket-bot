from __future__ import annotations

import datetime as dt
import sqlite3
import textwrap
from typing import Callable

from bot import db
from bot.config import Settings
from bot.feeds.kalshi import KalshiFeedClient
from bot.feeds.odds_api import OddsApiFeedClient
from bot.feeds.polymarket import PolymarketRestClient, filter_by_leagues
from bot.matching.matcher import (
    find_candidate_pairs_by_category,
    find_odds_api_pairs,
    store_odds_pairs,
    store_pairs,
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def pairs_scan(
    settings: Settings,
    polymarket_category: str,
    kalshi_series_ticker: str,
    similarity_threshold: float,
    date_tolerance_days: int,
    allow_live: bool,
    polymarket_leagues: list[str] | None = None,
    match_category: str | None = None,
) -> None:
    if not settings.data_collection_enabled and not allow_live:
        print(
            "data_collection_enabled is false in config.yaml — no live calls made. "
            "Pass --allow-live for a one-off manual scan, or set the flag true (VPS only)."
        )
        return

    pm_client = PolymarketRestClient(settings.feeds.polymarket.rest_rate_limit_per_sec)
    kalshi_client = KalshiFeedClient(settings.feeds.kalshi.base_url, settings.feeds.kalshi.poll_seconds)
    try:
        pm_markets = await pm_client.discover_markets([polymarket_category], closed=False)
        # Polymarket's "category" is always just "sports" regardless of
        # league (confirmed elsewhere in this codebase), so without a
        # league filter this compares every sport's markets against a
        # single-league Kalshi series — team-name/city token overlap alone
        # produces real cross-sport false matches (e.g. NFL "Kansas City"
        # vs MLB "Kansas City" both matching a Kalshi baseball market).
        if polymarket_leagues:
            pm_markets = filter_by_leagues(pm_markets, polymarket_leagues)
        match_category = match_category or "sports"
        if match_category == "sports":
            # Kalshi's KX*GAME series are plain moneyline ("Team A vs Team
            # B Winner?") — Polymarket's sports category also lists spread/
            # total/prop/season-futures markets for the same two teams,
            # which the matcher's team-name+date scoring can't tell apart
            # from the moneyline one (confirmed live: a "cover -2.5" spread
            # market scored 1.0 against a Kalshi winner market). Those
            # resolve on a different condition than a straight win/loss, so
            # pairing them against a moneyline market isn't an arb, it's a
            # coin flip. This filter is sports-specific: "futures" means
            # something completely different (and correct to match) in
            # e.g. politics/crypto, where it's the only market type Kalshi
            # offers for the equivalent single-outcome event.
            pm_markets = [m for m in pm_markets if m.get("marketType") == "moneyline"]
        kalshi_markets = await kalshi_client.get_markets(kalshi_series_ticker, status="open")
    finally:
        await pm_client.aclose()
        await kalshi_client.aclose()

    print(f"Polymarket US: {len(pm_markets)} open markets in category={polymarket_category!r}")
    print(f"Kalshi: {len(kalshi_markets)} open markets in series={kalshi_series_ticker!r}")

    proposals = find_candidate_pairs_by_category(match_category, pm_markets, kalshi_markets, similarity_threshold, date_tolerance_days)
    print(f"Found {len(proposals)} candidate pairs above similarity_threshold={similarity_threshold} (category={match_category!r})")

    conn = db.connect()
    inserted = store_pairs(conn, proposals)
    print(f"Stored {inserted} new unverified pairs. Run `bot pairs-review` to approve them.")


async def odds_pairs_scan(
    settings: Settings,
    polymarket_category: str,
    odds_sport_key: str,
    similarity_threshold: float,
    date_tolerance_days: int,
    allow_live: bool,
) -> None:
    if not settings.data_collection_enabled and not allow_live:
        print(
            "data_collection_enabled is false in config.yaml — no live calls made. "
            "Pass --allow-live for a one-off manual scan, or set the flag true (VPS only)."
        )
        return

    pm_client = PolymarketRestClient(settings.feeds.polymarket.rest_rate_limit_per_sec)
    odds_client = OddsApiFeedClient(settings.odds_api_key, settings.feeds.odds_api.base_url, settings.feeds.odds_api.regions)
    try:
        pm_markets = await pm_client.discover_markets([polymarket_category], closed=False)
        games = await odds_client.get_odds(odds_sport_key)
    finally:
        await pm_client.aclose()
        await odds_client.aclose()

    print(f"Polymarket US: {len(pm_markets)} open markets in category={polymarket_category!r}")
    print(f"Odds API: {len(games)} games in sport={odds_sport_key!r}")

    proposals = find_odds_api_pairs(pm_markets, games, similarity_threshold, date_tolerance_days)
    print(f"Found {len(proposals)} candidate pairs above similarity_threshold={similarity_threshold}")

    conn = db.connect()
    inserted = store_odds_pairs(conn, proposals)
    print(f"Stored {inserted} new unverified odds_pairs. Run `bot odds-pairs-review` to approve them.")


def _interactive_review(
    conn: sqlite3.Connection,
    table: str,
    rows: list[tuple],
    print_row: Callable[[tuple], None],
    *,
    get_category: Callable[[tuple], str | None] | None = None,
    observe_only_categories: set[str] | None = None,
) -> None:
    if not rows:
        print("No unverified pairs pending review.")
        return

    print(f"{len(rows)} unverified pair(s) to review. For each: [y]es approve, [n]o reject+delete, [s]kip, [q]uit.\n")

    for row in rows:
        pair_id = row[0]
        print_row(row)

        category = get_category(row) if get_category else None
        is_observe_only = bool(observe_only_categories) and category in observe_only_categories
        if is_observe_only:
            print(f"category={category!r} is observe-only (config.yaml universe.observe_only_categories) — "
                  f"cannot be verified, [n]o reject or [s]kip only.\n")

        while True:
            prompt = "[n]o reject / [s]kip / [q]uit: " if is_observe_only else "Approve this pair? [y/n/s/q]: "
            choice = input(prompt).strip().lower()
            valid = ("n", "s", "q") if is_observe_only else ("y", "n", "s", "q")
            if choice in valid:
                break
            print(f"Please enter one of: {', '.join(valid)}.")

        if choice == "q":
            print("Stopping review.")
            return
        if choice == "s":
            continue
        if choice == "y":
            # Enforced in code, not just the prompt above — never trust a
            # UI-layer check alone for something this task calls "never
            # weaken the verified gate."
            if is_observe_only:
                print(f"refused: category={category!r} is observe-only, cannot be verified.\n")
                continue
            conn.execute(f"UPDATE {table} SET verified = 1, reviewed_at = ? WHERE id = ?", (_now(), pair_id))
            print("approved.\n")
        elif choice == "n":
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (pair_id,))
            print("rejected and deleted.\n")
        conn.commit()


def pairs_review(
    conn: sqlite3.Connection,
    category: str | None = None,
    max_tier: int | None = None,
    observe_only_categories: set[str] | None = None,
) -> None:
    """--category/--max-tier (§8) surface the highest-value candidates
    first: sorted by each pair's most recent annualized_return (from
    pair_evaluations — locked_pair_arb scores unverified Tier B candidates
    too, see §7) descending, non-null first."""
    where = ["p.verified = 0"]
    params: list = []
    if category:
        where.append("p.category = ?")
        params.append(category)
    if max_tier is not None:
        where.append("(p.tier IS NULL OR p.tier <= ?)")
        params.append(max_tier)

    rows = conn.execute(
        "SELECT p.id, p.polymarket_slug, p.kalshi_ticker, p.similarity_score, p.polymarket_question, "
        "p.polymarket_description, p.polymarket_end_date, p.kalshi_title, p.kalshi_rules, p.kalshi_close_date, "
        "p.category, p.tier, "
        "(SELECT pe.annualized_return FROM pair_evaluations pe WHERE pe.pair_id = p.id "
        " AND pe.annualized_return IS NOT NULL ORDER BY pe.ts DESC LIMIT 1) AS latest_annual_return "
        f"FROM pairs p WHERE {' AND '.join(where)} "
        "ORDER BY (latest_annual_return IS NULL), latest_annual_return DESC, p.similarity_score DESC",
        params,
    ).fetchall()

    def print_row(row: tuple) -> None:
        (pair_id, pm_slug, k_ticker, score, pm_q, pm_desc, pm_end, k_title, k_rules, k_close,
         pair_category, tier, annual_return) = row
        print("=" * 100)
        annual_str = f"{annual_return:.1%}" if annual_return is not None else "n/a"
        print(f"pair #{pair_id}  category={pair_category}  tier={tier}  similarity={score}  latest_annual_return={annual_str}")
        print("-" * 100)
        print(f"POLYMARKET  slug={pm_slug}  ends={pm_end}")
        print(f"  question: {pm_q}")
        print(f"  resolution criteria:\n{textwrap.indent(textwrap.fill(pm_desc or '(none)', 96), '    ')}")
        print("-" * 100)
        print(f"KALSHI      ticker={k_ticker}  closes={k_close}")
        print(f"  title: {k_title}")
        print(f"  resolution criteria:\n{textwrap.indent(textwrap.fill(k_rules or '(none)', 96), '    ')}")
        print("=" * 100)

    _interactive_review(
        conn, "pairs", rows, print_row,
        get_category=lambda row: row[10],
        observe_only_categories=observe_only_categories,
    )


def odds_pairs_review(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, polymarket_slug, odds_api_game_id, long_team, similarity_score, polymarket_question, "
        "polymarket_description, polymarket_end_date, odds_api_matchup, odds_api_commence_time "
        "FROM odds_pairs WHERE verified = 0 ORDER BY similarity_score DESC"
    ).fetchall()

    def print_row(row: tuple) -> None:
        (pair_id, pm_slug, game_id, long_team, score, pm_q, pm_desc, pm_end, matchup, commence) = row
        print("=" * 100)
        print(f"pair #{pair_id}  similarity={score}  long_team={long_team}")
        print("-" * 100)
        print(f"POLYMARKET  slug={pm_slug}  ends={pm_end}")
        print(f"  question: {pm_q}")
        print(f"  resolution criteria:\n{textwrap.indent(textwrap.fill(pm_desc or '(none)', 96), '    ')}")
        print("-" * 100)
        print(f"ODDS API    game_id={game_id}  commences={commence}")
        print(f"  matchup: {matchup}")
        print("=" * 100)

    _interactive_review(conn, "odds_pairs", rows, print_row)
