from __future__ import annotations

import datetime as dt
import sqlite3
import textwrap

from bot import db
from bot.config import Settings
from bot.feeds.kalshi import KalshiFeedClient
from bot.feeds.polymarket import PolymarketRestClient
from bot.matching.matcher import find_candidate_pairs, store_pairs


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def pairs_scan(
    settings: Settings,
    polymarket_category: str,
    kalshi_series_ticker: str,
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
    kalshi_client = KalshiFeedClient(settings.feeds.kalshi.base_url, settings.feeds.kalshi.poll_seconds)
    try:
        pm_markets = await pm_client.discover_markets([polymarket_category], closed=False)
        kalshi_markets = await kalshi_client.get_markets(kalshi_series_ticker, status="open")
    finally:
        await pm_client.aclose()
        await kalshi_client.aclose()

    print(f"Polymarket US: {len(pm_markets)} open markets in category={polymarket_category!r}")
    print(f"Kalshi: {len(kalshi_markets)} open markets in series={kalshi_series_ticker!r}")

    proposals = find_candidate_pairs(pm_markets, kalshi_markets, similarity_threshold, date_tolerance_days)
    print(f"Found {len(proposals)} candidate pairs above similarity_threshold={similarity_threshold}")

    conn = db.connect()
    inserted = store_pairs(conn, proposals)
    print(f"Stored {inserted} new unverified pairs. Run `bot pairs review` to approve them.")


def pairs_review(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, polymarket_slug, kalshi_ticker, similarity_score, polymarket_question, "
        "polymarket_description, polymarket_end_date, kalshi_title, kalshi_rules, kalshi_close_date "
        "FROM pairs WHERE verified = 0 ORDER BY similarity_score DESC"
    ).fetchall()

    if not rows:
        print("No unverified pairs pending review.")
        return

    print(f"{len(rows)} unverified pair(s) to review. For each: [y]es approve, [n]o reject+delete, [s]kip, [q]uit.\n")

    for row in rows:
        (pair_id, pm_slug, k_ticker, score, pm_q, pm_desc, pm_end, k_title, k_rules, k_close) = row
        print("=" * 100)
        print(f"pair #{pair_id}  similarity={score}")
        print("-" * 100)
        print(f"POLYMARKET  slug={pm_slug}  ends={pm_end}")
        print(f"  question: {pm_q}")
        print(f"  resolution criteria:\n{textwrap.indent(textwrap.fill(pm_desc or '(none)', 96), '    ')}")
        print("-" * 100)
        print(f"KALSHI      ticker={k_ticker}  closes={k_close}")
        print(f"  title: {k_title}")
        print(f"  resolution criteria:\n{textwrap.indent(textwrap.fill(k_rules or '(none)', 96), '    ')}")
        print("=" * 100)

        while True:
            choice = input("Approve this pair? [y/n/s/q]: ").strip().lower()
            if choice in ("y", "n", "s", "q"):
                break
            print("Please enter y, n, s, or q.")

        if choice == "q":
            print("Stopping review.")
            return
        if choice == "s":
            continue
        if choice == "y":
            conn.execute(
                "UPDATE pairs SET verified = 1, reviewed_at = ? WHERE id = ?", (_now(), pair_id)
            )
            print("approved.\n")
        elif choice == "n":
            conn.execute("DELETE FROM pairs WHERE id = ?", (pair_id,))
            print("rejected and deleted.\n")
        conn.commit()
