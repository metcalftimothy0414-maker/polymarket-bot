"""Tiered polling with rate-limit enforcement (§6 of the category
expansion task).

Confirmed live scale (2026-08-09): 766,612 open Kalshi markets across
~12.6k series, 15,243 open Polymarket US markets. A flat poll cadence
across that universe would get rate-limited or banned — this module
enforces a hard requests/minute cap per venue and classifies every pair
into a polling tier so book-level polling only happens for pairs worth it.

Tier A: verified pairs — current cadence, unchanged, never touched here.
Tier B: unverified candidates whose match-time tier (§4) is at or below
        max_reviewable_tier — the "coarse pre-filter" the task names is
        that tier assignment itself, not a separate live-edge check
        (Tier C pairs have no book access to observe edge from in the
        first place, so live-edge-based promotion would be circular).
Tier C: everything else — metadata-level only (catalog discovery, §2),
        no book polling at all.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

DEMOTION_LOOKBACK_EVALUATIONS = 20
DEMOTION_MIN_EVALUATIONS = 10


@dataclass
class RateBudget:
    """Sliding 60-second window request counter. try_acquire() is the only
    way to "spend" budget — callers must check it before every request,
    not just log after the fact, or the cap is advisory instead of real."""

    max_requests_per_minute: int
    _timestamps: list[float] = field(default_factory=list)

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.pop(0)

    def try_acquire(self, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        self._prune(now)
        if len(self._timestamps) >= self.max_requests_per_minute:
            return False
        self._timestamps.append(now)
        return True

    def current_rate(self, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        self._prune(now)
        return len(self._timestamps)

    def utilization(self, now: float | None = None) -> float:
        if self.max_requests_per_minute <= 0:
            return 0.0
        return self.current_rate(now) / self.max_requests_per_minute


def classify_pair_tier(*, verified: bool, tier: int | None, max_reviewable_tier: int) -> str:
    """A if verified (trading-eligible, unaffected by anything here), B if
    the match-time divergence tier clears the reviewable threshold
    (tracked but not yet verified), C otherwise. A pair with tier=None
    (verified before §4 tiering existed, or a data gap) is treated as
    reviewable — absence of a red flag isn't the same as a known-bad one."""
    if verified:
        return "A"
    if tier is None or tier <= max_reviewable_tier:
        return "B"
    return "C"


def poll_interval_seconds(polling_tier: str, *, tier_a_seconds: float, tier_b_seconds: float, tier_c_seconds: float) -> float:
    return {"A": tier_a_seconds, "B": tier_b_seconds, "C": tier_c_seconds}[polling_tier]


def refresh_polling_tiers(conn: sqlite3.Connection, *, max_reviewable_tier: int) -> dict[str, int]:
    """Re-derives pairs.polling_tier for every row from its current
    verified/tier state. Idempotent — safe to call on every catalog
    refresh cycle. Returns counts per tier for logging/metrics."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, verified, tier FROM pairs").fetchall()
    counts = {"A": 0, "B": 0, "C": 0}
    for row in rows:
        polling_tier = classify_pair_tier(verified=bool(row["verified"]), tier=row["tier"], max_reviewable_tier=max_reviewable_tier)
        counts[polling_tier] += 1
        conn.execute("UPDATE pairs SET polling_tier = ? WHERE id = ?", (polling_tier, row["id"]))
    conn.commit()
    return counts


def demote_inactive_tier_b_pairs(conn: sqlite3.Connection) -> int:
    """A Tier B pair whose last DEMOTION_LOOKBACK_EVALUATIONS evaluations
    never once cleared gross_edge > 0 is spending 60s-cadence book-polling
    budget on a pair that's never shown any sign of an opportunity —
    demote it to C (metadata-only) until something about it changes (a
    rules-text edit resets tier via §3's re-matching, or an operator
    re-verifies it directly). Only considers pairs with at least
    DEMOTION_MIN_EVALUATIONS logged, so a pair doesn't get demoted before
    it's had a fair number of looks."""
    conn.row_factory = sqlite3.Row
    candidates = conn.execute(
        "SELECT id FROM pairs WHERE polling_tier = 'B' AND verified = 0"
    ).fetchall()
    demoted = 0
    for row in candidates:
        pair_id = row["id"]
        recent = conn.execute(
            "SELECT gross_edge_per_contract FROM pair_evaluations WHERE pair_id = ? "
            "ORDER BY ts DESC LIMIT ?",
            (pair_id, DEMOTION_LOOKBACK_EVALUATIONS),
        ).fetchall()
        if len(recent) < DEMOTION_MIN_EVALUATIONS:
            continue
        if all((r[0] is None or r[0] <= 0) for r in recent):
            conn.execute("UPDATE pairs SET polling_tier = 'C' WHERE id = ?", (pair_id,))
            demoted += 1
    conn.commit()
    return demoted
