from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass

# Generic words that carry no team/event-identifying signal; stripping them
# sharpens the token-overlap score toward what actually distinguishes one
# matchup from another (team names, not "who wins the game").
STOPWORDS = {
    "the", "a", "an", "vs", "vs.", "v", "at", "in", "on", "of", "to",
    "will", "game", "match", "moneyline", "winner", "wins", "win", "beat", "who", "be",
}


def normalize_tokens(text: str) -> set[str]:
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return {t for t in text.split() if t and t not in STOPWORDS}


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


def find_candidate_pairs(
    polymarket_markets: list[dict],
    kalshi_markets: list[dict],
    similarity_threshold: float = 0.6,
    date_tolerance_days: int = 0,
) -> list[ProposedPair]:
    """Match markets across venues on token overlap AND matching resolution date.

    Both conditions are required — title similarity alone produces false
    matches between same-day games with similar phrasing, and date alone
    matches unrelated events on a busy sports day.
    """
    pm_indexed = [
        # gameStartTime is the actual event date; endDate is a resolution deadline
        # that can be ~2 weeks after the event (confirmed against live markets) —
        # matching on endDate would silently never align with another venue's game date.
        (m, normalize_tokens(m["question"]), _date_only(m.get("gameStartTime") or m.get("endDate")))
        for m in polymarket_markets
    ]

    proposals: list[ProposedPair] = []
    for km in kalshi_markets:
        k_text = f"{km.get('title', '')} {km.get('subtitle', '')}".strip()
        k_tokens = normalize_tokens(k_text)
        # occurrence_datetime is the actual game/event time. close_time and
        # expiration_time are the administrative settlement deadline, which
        # for sports markets carries a ~2-3 day postponement-rescheduling
        # buffer past the real game (confirmed live against KXMLBGAME:
        # occurrence_datetime 08-10, close_time 08-13/14). Matching on
        # close_time silently pairs a market against the WRONG game in a
        # multi-game series once that buffer happens to land inside
        # date_tolerance_days of a different, later game on the other venue.
        k_date = _date_only(km.get("occurrence_datetime") or km.get("close_time") or km.get("expiration_time"))

        for pm, pm_tokens, pm_date in pm_indexed:
            if pm_date is None or k_date is None:
                continue
            if date_tolerance_days == 0:
                if pm_date != k_date:
                    continue
            elif abs((dt.date.fromisoformat(pm_date) - dt.date.fromisoformat(k_date)).days) > date_tolerance_days:
                continue

            score = token_overlap_score(pm_tokens, k_tokens)
            if score >= similarity_threshold:
                proposals.append(ProposedPair(
                    polymarket_slug=pm["slug"],
                    kalshi_ticker=km["ticker"],
                    similarity_score=round(score, 4),
                    polymarket_question=pm["question"],
                    polymarket_description=pm.get("description", ""),
                    polymarket_end_date=pm.get("endDate"),
                    kalshi_title=k_text,
                    kalshi_rules=f"{km.get('rules_primary', '')} {km.get('rules_secondary', '')}".strip(),
                    kalshi_close_date=km.get("close_time"),
                ))

    proposals.sort(key=lambda p: p.similarity_score, reverse=True)
    return _drop_ambiguous_pairs(proposals)


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
            "polymarket_end_date, kalshi_title, kalshi_rules, kalshi_close_date, verified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (p.polymarket_slug, p.kalshi_ticker, p.similarity_score, p.polymarket_question, p.polymarket_description,
             p.polymarket_end_date, p.kalshi_title, p.kalshi_rules, p.kalshi_close_date, now),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted
