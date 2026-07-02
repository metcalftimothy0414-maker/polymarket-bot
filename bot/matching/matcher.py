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
        (m, normalize_tokens(m["question"]), _date_only(m.get("endDate")))
        for m in polymarket_markets
    ]

    proposals: list[ProposedPair] = []
    for km in kalshi_markets:
        k_text = f"{km.get('title', '')} {km.get('subtitle', '')}".strip()
        k_tokens = normalize_tokens(k_text)
        k_date = _date_only(km.get("close_time") or km.get("expiration_time"))

        for pm, pm_tokens, pm_date in pm_indexed:
            if pm_date is None or k_date is None:
                continue
            if date_tolerance_days == 0:
                if pm_date != k_date:
                    continue
            elif abs((dt.date.fromisoformat(pm_date) - dt.date.fromisoformat(k_date)).days) > date_tolerance_days:
                continue

            score = jaccard(pm_tokens, k_tokens)
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
    return proposals


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
