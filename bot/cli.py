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

    args = parser.parse_args()
    if args.command == "feeds-check":
        try:
            asyncio.run(feeds_check(args.seconds, args.allow_live))
        except SystemExit:
            raise
        except Exception as exc:
            logger.exception("feeds-check failed")
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
