"""Per-category normalizers (§3 of the category expansion task): generic
candidate generation stays in matcher.py, but *what counts as matching
tokens* and *which date field means "when does this resolve"* differs by
category. Each normalizer turns a raw venue market dict into a
NormalizedMarket the shared scorer in matcher.py can compare across venues
without knowing anything sport/econ/election-specific.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

STOPWORDS = {
    "the", "a", "an", "vs", "vs.", "v", "at", "in", "on", "of", "to",
    "will", "game", "match", "moneyline", "winner", "wins", "win", "beat", "who", "be",
}


def normalize_tokens(text: str) -> set[str]:
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return {t for t in text.split() if t and t not in STOPWORDS}


def _date_only(iso_str: str | None) -> str | None:
    return iso_str[:10] if iso_str else None


@dataclass
class NormalizedMarket:
    tokens: set[str]
    date: str | None
    text: str = ""
    extra: dict = field(default_factory=dict)


class CategoryNormalizer(Protocol):
    category: str

    def normalize_polymarket(self, market: dict) -> NormalizedMarket: ...
    def normalize_kalshi(self, market: dict) -> NormalizedMarket: ...


class SportsNormalizer:
    """Unchanged from the pre-refactor matcher — sports pair counts and
    match results must stay byte-identical (regression-tested)."""

    category = "sports"

    def normalize_polymarket(self, m: dict) -> NormalizedMarket:
        # gameStartTime is the actual event date; endDate is a resolution
        # deadline that can be ~2 weeks after the event.
        return NormalizedMarket(
            tokens=normalize_tokens(m["question"]),
            date=_date_only(m.get("gameStartTime") or m.get("endDate")),
        )

    def normalize_kalshi(self, m: dict) -> NormalizedMarket:
        text = f"{m.get('title', '')} {m.get('subtitle', '')}".strip()
        # occurrence_datetime is the real game time; close_time/
        # expiration_time carry a multi-day postponement buffer that
        # silently matches the wrong game in a multi-game series.
        return NormalizedMarket(
            tokens=normalize_tokens(text),
            date=_date_only(m.get("occurrence_datetime") or m.get("close_time") or m.get("expiration_time")),
            text=text,
        )


_MONTH_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{4})\b",
    re.IGNORECASE,
)
_QUARTER_RE = re.compile(r"\bq([1-4])\s+(\d{4})\b", re.IGNORECASE)
# A bare number (e.g. the "2026" in "July 2026") is not a threshold — only
# count a number that's flanked by an explicit comparison word or a
# %/percent/k/bps unit, otherwise "July 2026 CPI above 3.2%" would extract
# 2026 as the threshold instead of 3.2.
_THRESHOLD_RE = re.compile(
    r"(?:(above|below|over|under|greater than|less than|at least|at most)\s*([-+]?\d+\.?\d*)"
    r"|([-+]?\d+\.?\d*)\s*(%|percent|k|bps)\b)",
    re.IGNORECASE,
)
_REVISION_WORDS = {"revised", "revision", "preliminary", "initial", "final estimate", "unrevised"}


def _reference_period(text: str) -> str | None:
    m = _MONTH_RE.search(text)
    if m:
        return f"{m.group(1)[:3].lower()}-{m.group(2)}"
    m = _QUARTER_RE.search(text)
    if m:
        return f"q{m.group(1)}-{m.group(2)}"
    return None


def _threshold_value(text: str) -> float | None:
    m = _THRESHOLD_RE.search(text)
    if not m:
        return None
    number = m.group(2) or m.group(3)
    try:
        return float(number)
    except (TypeError, ValueError):
        return None


def _mentions_revision_handling(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _REVISION_WORDS)


class EconomicIndicatorNormalizer:
    """CPI, payrolls, GDP, unemployment, Fed decisions. Matches on release
    date (these markets resolve on the print date itself — no postponement-
    buffer gap like sports) plus reference period and threshold, both
    recorded in `extra` for the divergence-tier check in §4: whether either
    side specifies revision handling is the #1 divergence vector for econ
    data (a market that's silent on "what if BLS revises this number later"
    is a real resolution-rule gap, not a formality)."""

    category = "economic_indicator"

    def _normalize(self, text: str, date_field: str | None) -> NormalizedMarket:
        return NormalizedMarket(
            tokens=normalize_tokens(text),
            date=_date_only(date_field),
            text=text,
            extra={
                "reference_period": _reference_period(text),
                "threshold_value": _threshold_value(text),
                "mentions_revision_handling": _mentions_revision_handling(text),
            },
        )

    def normalize_polymarket(self, m: dict) -> NormalizedMarket:
        text = f"{m.get('question', '')} {m.get('description', '')}".strip()
        return self._normalize(text, m.get("endDate") or m.get("gameStartTime"))

    def normalize_kalshi(self, m: dict) -> NormalizedMarket:
        text = f"{m.get('title', '')} {m.get('rules_primary', '')} {m.get('rules_secondary', '')}".strip()
        return self._normalize(text, m.get("close_time") or m.get("expiration_time"))


_OFFICE_WORDS = {"president", "governor", "senate", "senator", "house", "representative", "mayor", "attorney general"}


def _office_tokens(text: str) -> set[str]:
    lowered = text.lower()
    return {w for w in _OFFICE_WORDS if w in lowered}


class PoliticsElectionsNormalizer:
    """Office, jurisdiction, date. Candidate-name aliasing (nicknames,
    "Trump" vs "Donald Trump") is NOT attempted here — token overlap on the
    full text catches exact-name matches; anything needing real alias
    resolution is exactly the kind of ambiguous case that should surface at
    a lower similarity score for human review, not be silently resolved by
    a hardcoded alias table that will always be incomplete."""

    category = "politics_elections"

    def _normalize(self, text: str, date_field: str | None) -> NormalizedMarket:
        return NormalizedMarket(
            tokens=normalize_tokens(text),
            date=_date_only(date_field),
            text=text,
            extra={"office": _office_tokens(text)},
        )

    def normalize_polymarket(self, m: dict) -> NormalizedMarket:
        text = f"{m.get('question', '')} {m.get('description', '')}".strip()
        return self._normalize(text, m.get("endDate") or m.get("gameStartTime"))

    def normalize_kalshi(self, m: dict) -> NormalizedMarket:
        text = f"{m.get('title', '')} {m.get('rules_primary', '')} {m.get('rules_secondary', '')}".strip()
        return self._normalize(text, m.get("close_time") or m.get("expiration_time"))


_OPERATOR_RE = re.compile(r"(≥|>=|at least)|(>)|(≤|<=|at most)|(<)", re.IGNORECASE)


def _comparison_operator(text: str) -> str | None:
    m = _OPERATOR_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return ">="
    if m.group(2):
        return ">"
    if m.group(3):
        return "<="
    if m.group(4):
        return "<"
    return None


# Captures a short proper-noun run (1-4 Capitalized tokens) right after the
# trigger phrase, then stops — "according to CoinDesk closing price" must
# not swallow "closing price" into the source name.
_SOURCE_RE = re.compile(r"(?:according to|per|sourced? (?:from|by))\s+([A-Z][A-Za-z0-9&.]*(?:\s+[A-Z][A-Za-z0-9&.]*){0,3})")


def _named_source(text: str) -> str | None:
    m = _SOURCE_RE.search(text)
    return m.group(1).strip() if m else None


class NumericThresholdNormalizer:
    """Any "will X be above/below N by date D" market. `≥` vs `>` and
    whether a named source is specified both matter for divergence risk —
    recorded in `extra`, not folded into the match score itself."""

    category = "numeric_threshold"

    def _normalize(self, text: str, date_field: str | None) -> NormalizedMarket:
        return NormalizedMarket(
            tokens=normalize_tokens(text),
            date=_date_only(date_field),
            text=text,
            extra={
                "threshold_value": _threshold_value(text),
                "comparison_operator": _comparison_operator(text),
                "named_source": _named_source(text),
            },
        )

    def normalize_polymarket(self, m: dict) -> NormalizedMarket:
        text = f"{m.get('question', '')} {m.get('description', '')}".strip()
        return self._normalize(text, m.get("endDate") or m.get("gameStartTime"))

    def normalize_kalshi(self, m: dict) -> NormalizedMarket:
        text = f"{m.get('title', '')} {m.get('rules_primary', '')} {m.get('rules_secondary', '')}".strip()
        return self._normalize(text, m.get("close_time") or m.get("expiration_time"))


class GenericNormalizer:
    """Fallback: token similarity + close-time proximity only, no
    structured extraction. Always the highest divergence tier (enforced in
    §4, not here) — this is the category assigned when nothing else fits,
    so the pairing has the least evidence behind it."""

    category = "generic"

    def normalize_polymarket(self, m: dict) -> NormalizedMarket:
        text = f"{m.get('question', '')} {m.get('description', '')}".strip()
        return NormalizedMarket(tokens=normalize_tokens(text), date=_date_only(m.get("endDate") or m.get("gameStartTime")), text=text)

    def normalize_kalshi(self, m: dict) -> NormalizedMarket:
        text = f"{m.get('title', '')} {m.get('rules_primary', '')} {m.get('rules_secondary', '')}".strip()
        return NormalizedMarket(tokens=normalize_tokens(text), date=_date_only(m.get("close_time") or m.get("expiration_time")), text=text)


NORMALIZERS: dict[str, CategoryNormalizer] = {
    "sports": SportsNormalizer(),
    "economic_indicator": EconomicIndicatorNormalizer(),
    "politics_elections": PoliticsElectionsNormalizer(),
    "numeric_threshold": NumericThresholdNormalizer(),
    "generic": GenericNormalizer(),
}


def normalizer_for(category: str) -> CategoryNormalizer:
    return NORMALIZERS.get(category, NORMALIZERS["generic"])
