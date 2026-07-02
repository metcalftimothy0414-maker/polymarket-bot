from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys

from bot import db
from bot.config import load_settings
from bot.feeds.auth import PolymarketAuth
from bot.feeds.polymarket import PolymarketRestClient, PolymarketWSClient
from bot.logging_conf import setup_logging
from bot.matching.cli import odds_pairs_review, odds_pairs_scan, pairs_review, pairs_scan
from bot.report import export_csv, print_report
from bot.runner import run as run_bot

logger = logging.getLogger(__name__)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def feeds_check(seconds: int, allow_live: bool) -> None:
    settings = load_settings()
    setup_logging(settings.log.level, settings.log.file)
    conn = db.connect()

    if not settings.data_collection_enabled and not allow_live:
        print(
            "data_collection_enabled is false in config.yaml — no live calls made. "
            "Pass --allow-live for a one-off manual check, or set the flag true "
            "(VPS only) to run for real."
        )
        return

    rest = PolymarketRestClient(settings.feeds.polymarket.rest_rate_limit_per_sec)
    try:
        markets = await rest.discover_markets(settings.feeds.polymarket.categories, closed=False)
        print(f"Discovered {len(markets)} open markets in categories={settings.feeds.polymarket.categories}")
        for m in markets[:5]:
            print(f"  {m['slug']:45s} {m['question']}")
            conn.execute(
                "INSERT OR REPLACE INTO markets (slug, question, category, end_date, active, closed, raw_json, discovered_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m["slug"], m["question"], m.get("category"), m.get("endDate"), int(m["active"]), int(m["closed"]),
                 "", _now(), _now()),
            )
        conn.commit()

        if not markets:
            print("No open markets found; nothing to stream.")
            return

        watchlist = settings.feeds.polymarket.watchlist_slugs or [markets[0]["slug"]]
        book = await rest.get_book(watchlist[0])
        print(f"\nREST book snapshot for {watchlist[0]}:")
        print(f"  best bid: {book['bids'][0] if book['bids'] else None}")
        print(f"  best ask: {book['offers'][0] if book['offers'] else None}")
    finally:
        await rest.aclose()

    auth = PolymarketAuth(settings.polymarket_api_key_id, settings.polymarket_private_key)
    ws = PolymarketWSClient(auth)
    stop_event = asyncio.Event()

    async def on_update(data: dict) -> None:
        best_bid = data["bids"][0]["px"]["value"] if data["bids"] else None
        best_ask = data["offers"][0]["px"]["value"] if data["offers"] else None
        print(f"[ws] {data['marketSlug']}: bid={best_bid} ask={best_ask}")

    async def stop_after(delay: float) -> None:
        await asyncio.sleep(delay)
        stop_event.set()

    print(f"\nStreaming WS order book for {watchlist} for {seconds}s...")
    await asyncio.gather(
        ws.stream(watchlist, on_update, stop_event=stop_event),
        stop_after(seconds),
    )
    conn.execute("INSERT INTO heartbeats (ts, component, detail) VALUES (?, ?, ?)", (_now(), "feeds_check", "manual run"))
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(prog="bot")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("feeds-check", help="One-off diagnostic: confirm live Polymarket data flows")
    check.add_argument("--seconds", type=int, default=20, help="How long to stream WS updates for")
    check.add_argument("--allow-live", action="store_true", help="Override data_collection_enabled=false for this run")

    scan = sub.add_parser("pairs-scan", help="Discover candidate cross-venue market pairs")
    scan.add_argument("--polymarket-category", required=True)
    scan.add_argument("--kalshi-series", required=True, help="Kalshi series_ticker, e.g. KXNBA")
    scan.add_argument("--similarity-threshold", type=float, default=0.6)
    scan.add_argument("--date-tolerance-days", type=int, default=0)
    scan.add_argument("--allow-live", action="store_true", help="Override data_collection_enabled=false for this run")

    sub.add_parser("pairs-review", help="Interactively approve/reject proposed pairs")

    odds_scan = sub.add_parser("odds-pairs-scan", help="Discover candidate Polymarket <-> Odds API pairs")
    odds_scan.add_argument("--polymarket-category", required=True)
    odds_scan.add_argument("--odds-sport-key", required=True, help="The Odds API sport_key, e.g. basketball_nba")
    odds_scan.add_argument("--similarity-threshold", type=float, default=0.6)
    odds_scan.add_argument("--date-tolerance-days", type=int, default=0)
    odds_scan.add_argument("--allow-live", action="store_true", help="Override data_collection_enabled=false for this run")

    sub.add_parser("odds-pairs-review", help="Interactively approve/reject proposed Odds API pairs")

    sub.add_parser("run", help="Run the live scan/paper-trade loop (gated by data_collection_enabled)")

    sub.add_parser("report", help="Print per-strategy metrics, kill criteria, and a side-by-side comparison")

    export = sub.add_parser("export", help="Dump the paper trade log as CSV")
    export.add_argument("--out", default="paper_trades.csv")

    args = parser.parse_args()
    try:
        if args.command == "run":
            settings = load_settings()
            setup_logging(settings.log.level, settings.log.file)
            asyncio.run(run_bot(settings))
        elif args.command == "feeds-check":
            asyncio.run(feeds_check(args.seconds, args.allow_live))
        elif args.command == "pairs-scan":
            settings = load_settings()
            setup_logging(settings.log.level, settings.log.file)
            asyncio.run(pairs_scan(
                settings, args.polymarket_category, args.kalshi_series,
                args.similarity_threshold, args.date_tolerance_days, args.allow_live,
            ))
        elif args.command == "pairs-review":
            settings = load_settings()
            setup_logging(settings.log.level, settings.log.file)
            conn = db.connect()
            pairs_review(conn)
        elif args.command == "odds-pairs-scan":
            settings = load_settings()
            setup_logging(settings.log.level, settings.log.file)
            asyncio.run(odds_pairs_scan(
                settings, args.polymarket_category, args.odds_sport_key,
                args.similarity_threshold, args.date_tolerance_days, args.allow_live,
            ))
        elif args.command == "odds-pairs-review":
            settings = load_settings()
            setup_logging(settings.log.level, settings.log.file)
            conn = db.connect()
            odds_pairs_review(conn)
        elif args.command == "report":
            conn = db.connect()
            print_report(conn)
        elif args.command == "export":
            conn = db.connect()
            count = export_csv(conn, args.out)
            if count:
                print(f"Exported {count} paper trade rows to {args.out}")
    except SystemExit:
        raise
    except Exception as exc:
        logger.exception("%s failed", args.command)
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
