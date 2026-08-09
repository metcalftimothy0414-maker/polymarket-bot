from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field

from bot.matching.normalizers import normalize_tokens, normalizer_for
from bot.matching.tiering import assign_tier

# Re-exported for backward compatibility — callers/tests that imported
# these directly from bot.matching.matcher before the §3 category-expansion
# refactor still work unchanged.
__all__ = [
    "ProposedPair", "ProposedOddsPair", "find_candidate_pairs", "find_candidate_pairs_by_category",
    "find_odds_api_pairs", "store_pairs", "store_odds_pairs", "normalize_tokens", "jaccard", "token_overlap_score",
]


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def token_overlap_score(a: set[str], b: set[str]) -> float:
    """Overlap coefficient: intersection / smaller set size.

    Used instead of Jaccard for cross-venue matching because venues disagree
    on naming convention (Polymarket "Lakers vs. Celtics" vs. Odds API's full
    "Los Angeles Lakers"/"Boston Celtics") — Jaccard punishes that size
    mismatch even on a perfect match. The verified/reviewed gate downstream
    is the real false-match safeguard, not this score, so being more
    permissive here (more candidates surfaced for a human to check) is the
    right tradeoff.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _date_only(iso_str: str | None) -> str | None:
    return iso_str[:10] if iso_str else None


def _dates_within_tolerance(pm_date: str, k_date: str, date_tolerance_days: int) -> bool:
    if date_tolerance_days == 0:
        return pm_date == k_date
    return abs((dt.date.fromisoformat(pm_date) - dt.date.fromisoformat(k_date)).days) <= date_tolerance_days


def _days_from_today(date_only: str | None) -> float | None:
    if date_only is None:
        return None
    return (dt.date.fromisoformat(date_only) - dt.datetime.now(dt.timezone.utc).date()).days


@dataclass
class ProposedPair:
    polymarket_slug: str
    kalshi_ticker: str
    similarity_score: float
    polymarket_question: str
    polymarket_description: str
    polymarket_end_date: str | None
    kalshi_title: str
    kalshi_rules: str
    kalshi_close_date: str | None
    category: str = "sports"
    tier: int = 1
    tier_reasons: str = "[]"


def find_candidate_pairs_by_category(
    category: str,
    polymarket_markets: list[dict],
    kalshi_markets: list[dict],
    similarity_threshold: float = 0.6,
    date_tolerance_days: int = 0,
) -> list[ProposedPair]:
    """Generic candidate generation: per-category normalizers turn each raw
    market into comparable tokens+date (bot.matching.normalizers), the
    scoring and ambiguity-rejection here are shared across every category.
    Both token overlap AND matching resolution date are required — title
    similarity alone produces false matches between same-day events with
    similar phrasing, and date alone matches unrelated events on a busy day.
    """
    normalizer = normalizer_for(category)
    pm_indexed = [(m, normalizer.normalize_polymarket(m)) for m in polymarket_markets]

    proposals: list[ProposedPair] = []
    for km in kalshi_markets:
        k_norm = normalizer.normalize_kalshi(km)
        if k_norm.date is None:
            continue

        for pm, pm_norm in pm_indexed:
            if pm_norm.date is None:
                continue
            if not _dates_within_tolerance(pm_norm.date, k_norm.date, date_tolerance_days):
                continue

            score = token_overlap_score(pm_norm.tokens, k_norm.tokens)
            if score >= similarity_threshold:
                tier_result = assign_tier(
                    category=category, pm=pm_norm, kalshi=k_norm,
                    days_to_resolution=_days_from_today(pm_norm.date),
                )
                proposals.append(ProposedPair(
                    polymarket_slug=pm["slug"],
                    kalshi_ticker=km["ticker"],
                    similarity_score=round(score, 4),
                    polymarket_question=pm["question"],
                    polymarket_description=pm.get("description", ""),
                    polymarket_end_date=pm.get("endDate"),
                    kalshi_title=k_norm.text,
                    kalshi_rules=f"{km.get('rules_primary', '')} {km.get('rules_secondary', '')}".strip(),
                    kalshi_close_date=km.get("close_time"),
                    category=category,
                    tier=tier_result.tier,
                    tier_reasons=json.dumps(tier_result.reasons),
                ))

    proposals.sort(key=lambda p: p.similarity_score, reverse=True)
    return _drop_ambiguous_pairs(proposals)


def find_candidate_pairs(
    polymarket_markets: list[dict],
    kalshi_markets: list[dict],
    similarity_threshold: float = 0.6,
    date_tolerance_days: int = 0,
) -> list[ProposedPair]:
    """Sports-category matching — kept as its own entry point (rather than
    folded into callers passing category="sports" everywhere) since this
    was the matcher's only behavior before the §3 category-expansion
    refactor and every existing caller/test still calls it by this name.
    Byte-identical output to the pre-refactor implementation."""
    return find_candidate_pairs_by_category("sports", polymarket_markets, kalshi_markets, similarity_threshold, date_tolerance_days)


def _drop_ambiguous_pairs(proposals: list[ProposedPair]) -> list[ProposedPair]:
    """A team can play the same opponent on consecutive days (a series), and
    a game near midnight ET can land on either UTC calendar date depending
    on start time — so one Kalshi ticker can satisfy the date+token check
    against more than one DISTINCT Polymarket market for the same matchup
    (confirmed live: 'Milwaukee vs San Diego' on two different days both
    matched the same Kalshi ticker). There's no way to tell from title+date
    alone which specific game a ticker refers to in that situation, so
    exclude the whole ambiguous group rather than guess — a wrong guess
    here is a false pair, not a rescheduling nuance."""
    kalshi_to_slugs: dict[str, set[str]] = {}
    slug_to_kalshis: dict[str, set[str]] = {}
    for p in proposals:
        kalshi_to_slugs.setdefault(p.kalshi_ticker, set()).add(p.polymarket_slug)
        slug_to_kalshis.setdefault(p.polymarket_slug, set()).add(p.kalshi_ticker)

    return [
        p for p in proposals
        if len(kalshi_to_slugs[p.kalshi_ticker]) == 1 and len(slug_to_kalshis[p.polymarket_slug]) == 1
    ]


def _long_team_name(pm_market: dict) -> str | None:
    """The Polymarket market's order book prices the 'long' side of marketSides —
    that's the team whose win probability we need from the other venue to compare."""
    for side in pm_market.get("marketSides", []):
        if side.get("long") and side.get("team"):
            return side["team"].get("name")
    return None


@dataclass
class ProposedOddsPair:
    polymarket_slug: str
    odds_api_game_id: str
    odds_api_sport_key: str
    long_team: str
    similarity_score: float
    polymarket_question: str
    polymarket_description: str
    polymarket_end_date: str | None
    odds_api_matchup: str
    odds_api_commence_time: str | None


def find_odds_api_pairs(
    polymarket_markets: list[dict],
    odds_games: list[dict],
    similarity_threshold: float = 0.6,
    date_tolerance_days: int = 0,
) -> list[ProposedOddsPair]:
    pm_indexed = [
        (m, normalize_tokens(m["question"]), _date_only(m.get("gameStartTime") or m.get("endDate")), _long_team_name(m))
        for m in polymarket_markets
    ]

    proposals: list[ProposedOddsPair] = []
    for game in odds_games:
        matchup = f"{game.get('home_team', '')} {game.get('away_team', '')}".strip()
        g_tokens = normalize_tokens(matchup)
        g_date = _date_only(game.get("commence_time"))

        for pm, pm_tokens, pm_date, long_team in pm_indexed:
            if pm_date is None or g_date is None or long_team is None:
                continue
            if date_tolerance_days == 0:
                if pm_date != g_date:
                    continue
            elif abs((dt.date.fromisoformat(pm_date) - dt.date.fromisoformat(g_date)).days) > date_tolerance_days:
                continue

            score = token_overlap_score(pm_tokens, g_tokens)
            if score >= similarity_threshold:
                proposals.append(ProposedOddsPair(
                    polymarket_slug=pm["slug"],
                    odds_api_game_id=game["id"],
                    odds_api_sport_key=game.get("sport_key", ""),
                    long_team=long_team,
                    similarity_score=round(score, 4),
                    polymarket_question=pm["question"],
                    polymarket_description=pm.get("description", ""),
                    polymarket_end_date=pm.get("endDate"),
                    odds_api_matchup=matchup,
                    odds_api_commence_time=game.get("commence_time"),
                ))

    proposals.sort(key=lambda p: p.similarity_score, reverse=True)
    return proposals


def store_odds_pairs(conn: sqlite3.Connection, pairs: list[ProposedOddsPair]) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    inserted = 0
    for p in pairs:
        cur = conn.execute(
            "INSERT OR IGNORE INTO odds_pairs "
            "(polymarket_slug, odds_api_game_id, odds_api_sport_key, long_team, similarity_score, "
            "polymarket_question, polymarket_description, polymarket_end_date, odds_api_matchup, "
            "odds_api_commence_time, verified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (p.polymarket_slug, p.odds_api_game_id, p.odds_api_sport_key, p.long_team, p.similarity_score,
             p.polymarket_question, p.polymarket_description, p.polymarket_end_date, p.odds_api_matchup,
             p.odds_api_commence_time, now),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def store_pairs(conn: sqlite3.Connection, pairs: list[ProposedPair]) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    inserted = 0
    for p in pairs:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pairs "
            "(polymarket_slug, kalshi_ticker, similarity_score, polymarket_question, polymarket_description, "
            "polymarket_end_date, kalshi_title, kalshi_rules, kalshi_close_date, category, tier, tier_reasons, "
            "verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (p.polymarket_slug, p.kalshi_ticker, p.similarity_score, p.polymarket_question, p.polymarket_description,
             p.polymarket_end_date, p.kalshi_title, p.kalshi_rules, p.kalshi_close_date, p.category,
             p.tier, p.tier_reasons, now),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted
