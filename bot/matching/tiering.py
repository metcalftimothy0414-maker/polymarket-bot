"""Divergence risk tier assignment (§4 of the category expansion task).

Every matched pair gets a tier 1-5 at match time, with structured reasons
recorded as JSON — not just a number, since the reasons are what a human
reviewer (or a future recalibration pass) actually needs. Tier 1 is the
most confidence-inspiring (unambiguous both sides, agreeing named source,
no discretionary language); tier 5 is the least. Higher tier = more likely
the two venues resolve differently.

Takes the two sides' NormalizedMarket objects (bot.matching.normalizers)
directly rather than re-deriving comparison operators / named sources /
revision-handling from raw text a second time — the normalizer already did
that extraction once, this module just compares it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bot.matching.normalizers import NormalizedMarket

MAX_DAYS_TO_RESOLUTION_FOR_BASE_TIER = 90

_DISCRETIONARY_PHRASES = (
    "at exchange discretion", "at the exchange's discretion", "reasonably",
    "generally", "in the exchange's judgment", "substantially", "at its discretion",
)
_OFFICIAL_UNNAMED_RE = re.compile(r"\bofficial\b(?!\s+(?:source|website|record)s?\s+(?:of|from|at)\s+\w)", re.IGNORECASE)
_CANCELLATION_WORDS = ("cancel", "postpone", "void", "push", "tie")
_TIMEZONE_RE = re.compile(r"\b(ET|EST|EDT|UTC|GMT|PT|PST|PDT|CT|CST|CDT)\b")


@dataclass
class TierResult:
    tier: int
    reasons: list[str]


def _mentions_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(w in lowered for w in words)


def _has_discretionary_language(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _DISCRETIONARY_PHRASES)


def _has_unnamed_official_source(text: str) -> bool:
    """"the official result" with no named body attached is a red flag;
    "official result from ESPN" is fine — the regex's negative lookahead
    only excludes the pattern when a source clearly follows "official"."""
    return bool(_OFFICIAL_UNNAMED_RE.search(text))


def _has_explicit_timezone(text: str) -> bool:
    return bool(_TIMEZONE_RE.search(text))


def assign_tier(
    *,
    category: str,
    pm: NormalizedMarket,
    kalshi: NormalizedMarket,
    days_to_resolution: float | None,
) -> TierResult:
    """Starts at tier 1 and raises it on each signal present — never lowers
    below what the signals justify, so a pair with zero red flags still
    lands at tier 1 rather than some conservative default."""
    reasons: list[str] = []
    tier = 1
    combined = f"{pm.text or ''} {kalshi.text or ''}"

    if category == "generic":
        # No structured extraction at all — the least evidence behind any
        # matched pair, so this alone earns the worst tier rather than
        # sharing a floor with pairs that at least have category-specific
        # signals (named source, comparison operator, ...) to check.
        reasons.append("matched_by_generic_normalizer")
        tier = max(tier, 5)

    if _has_unnamed_official_source(combined):
        reasons.append("unnamed_official_source")
        tier = max(tier, 3)

    if _has_discretionary_language(combined):
        reasons.append("discretionary_language")
        tier = max(tier, 4)

    pm_has_tz = _has_explicit_timezone(pm.text or "")
    k_has_tz = _has_explicit_timezone(kalshi.text or "")
    if not pm_has_tz or not k_has_tz:
        reasons.append("missing_explicit_timezone")
        tier = max(tier, 2)

    if not _mentions_any(pm.text or "", _CANCELLATION_WORDS) or not _mentions_any(kalshi.text or "", _CANCELLATION_WORDS):
        reasons.append("missing_cancellation_postponement_handling")
        tier = max(tier, 3)

    pm_source = pm.extra.get("named_source")
    k_source = kalshi.extra.get("named_source")
    if pm_source and k_source and pm_source.lower() != k_source.lower():
        reasons.append(f"different_named_sources:{pm_source}!={k_source}")
        tier = max(tier, 4)

    pm_op = pm.extra.get("comparison_operator")
    k_op = kalshi.extra.get("comparison_operator")
    if pm_op and k_op and pm_op != k_op:
        reasons.append(f"comparison_operator_mismatch:{pm_op}!={k_op}")
        tier = max(tier, 3)
    elif pm_op is None and k_op is None and category == "numeric_threshold":
        reasons.append("comparison_operator_unspecified")
        tier = max(tier, 2)

    if category == "economic_indicator":
        pm_revision = pm.extra.get("mentions_revision_handling", False)
        k_revision = kalshi.extra.get("mentions_revision_handling", False)
        if not pm_revision or not k_revision:
            reasons.append("missing_revision_handling")
            tier = max(tier, 4)  # the task calls this the #1 divergence vector for econ data

        pm_period = pm.extra.get("reference_period")
        k_period = kalshi.extra.get("reference_period")
        if pm_period and k_period and pm_period != k_period:
            reasons.append(f"reference_period_mismatch:{pm_period}!={k_period}")
            tier = max(tier, 4)

    if days_to_resolution is not None and days_to_resolution > MAX_DAYS_TO_RESOLUTION_FOR_BASE_TIER:
        reasons.append(f"time_to_resolution_over_{MAX_DAYS_TO_RESOLUTION_FOR_BASE_TIER}_days")
        tier = max(tier, 3)

    return TierResult(tier=min(tier, 5), reasons=reasons)
