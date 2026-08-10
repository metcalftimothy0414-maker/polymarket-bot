"""Divergence persistence tracking (§7 of the category expansion task) —
the measurement that justifies the whole task: does divergence exist
outside sports, and does it persist longer there?

For every pair where gross_edge > 0 on a given scan, sample until it
closes (gross_edge <= 0, or the pair stops being evaluated) and record how
long it lasted and how large it got.
"""
from __future__ import annotations

import datetime as dt
import sqlite3


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _open_period(conn: sqlite3.Connection, pair_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM divergence_periods WHERE pair_id = ? AND closed_at IS NULL "
        "ORDER BY opened_at DESC LIMIT 1",
        (pair_id,),
    ).fetchone()


def update_divergence_period(
    conn: sqlite3.Connection,
    *,
    pair_id: int,
    category: str,
    tier: int,
    gross_edge: float | None,
    now: str | None = None,
) -> None:
    """Call once per (pair, evaluation). A positive gross_edge opens a new
    period if none is open, or extends peak_edge on the currently open one.
    A non-positive (or missing — e.g. a stale-book skip) edge closes
    whatever period is currently open, since the divergence is no longer
    observably present."""
    now = now or _now()
    open_period = _open_period(conn, pair_id)
    is_diverging = gross_edge is not None and gross_edge > 0

    if is_diverging:
        if open_period is None:
            conn.execute(
                "INSERT INTO divergence_periods (pair_id, category, tier, opened_at, peak_edge) "
                "VALUES (?, ?, ?, ?, ?)",
                (pair_id, category, tier, now, gross_edge),
            )
        elif gross_edge > open_period["peak_edge"]:
            conn.execute(
                "UPDATE divergence_periods SET peak_edge = ? WHERE id = ?",
                (gross_edge, open_period["id"]),
            )
    elif open_period is not None:
        opened = dt.datetime.fromisoformat(open_period["opened_at"])
        closed = dt.datetime.fromisoformat(now)
        duration = (closed - opened).total_seconds()
        conn.execute(
            "UPDATE divergence_periods SET closed_at = ?, duration_seconds = ? WHERE id = ?",
            (now, duration, open_period["id"]),
        )
    conn.commit()
