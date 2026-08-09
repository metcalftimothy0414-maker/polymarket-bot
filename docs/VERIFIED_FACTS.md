# Verified Facts

Re-verification of fee formulas, endpoints, and other snapshot facts used by
the arbitrage extension, per the build prompt's Phase 0 requirement. Where a
live source disagrees with a prior assumption, the live source wins and the
discrepancy is called out.

## Kalshi fees

- **Taker**: `fee = roundup(M_taker × 0.07 × C × P × (1−P))`, `M_taker` default 1.
- **Maker**: `fee = roundup(M_maker × 0.0175 × C × P × (1−P))`, `M_maker` default 0.
- Rounding: fee + positionCost rounds up to the nearest centicent ($0.0001).
- Peak fee at P=$0.50: $1.75 per 100 taker contracts.
- Source: [kalshi.com/docs/kalshi-fee-schedule.pdf](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
  ("Fee Schedule for July 2026 — 7.7.26 Update"), corroborated via web search
  (Market Math, whirligigbear.substack.com summaries citing the same PDF).
  Fetched 2026-08-09.
- **Not independently verified**: the per-series non-standard-fee multiplier
  table (which series carry `M_maker=1` instead of 0, which are 0/0). Direct
  fetch of the PDF failed in this environment (TLS certificate verification
  error) and no secondary source enumerated the full table. **Do not
  hardcode `M_maker=0` for any series without confirming it against the live
  PDF or API first** — the fee engine defaults conservatively to
  `M_maker=1` (see `bot/edge.py`) until an operator populates a real
  per-series override table.

## Polymarket US fees

- **Taker**: `fee = 0.06 × C × p × (1−p)`, max $1.50/100 contracts at p=$0.50.
- **Maker rebate**: `rebate = 0.0125 × C × p × (1−p)`, max $0.31/100 contracts credited at p=$0.50.
- Rounding: banker's rounding (round-half-to-even) to the nearest $0.01.
- Multi-fill orders: each fill's fee is independently banker's-rounded, then
  adjusted (downward only) so the sum never exceeds the banker's-rounded
  cumulative exact fee. Maker rebates are computed per fill, independently.
- Taker volume rebate tiers (prior calendar-month notional): $250K–$999,999 →
  10%; $1M–$9,999,999 → 25%; $10M+ → 50%. This system starts at $0 prior
  volume — model zero taker rebate.
- Effective: July 1, 2026 (exchange-wide, 12 AM ET).
- Source: [docs.polymarket.us/fees](https://docs.polymarket.us/fees). Fetched 2026-08-09.
- **Discrepancy vs. existing `bot/fees.py`**: the pre-existing fee module used
  `float` for the theta coefficients and rounded to a whole cent implicitly
  via float math — it did not implement banker's rounding or the multi-fill
  cumulative cap. `bot/edge.py` (new) implements the exact rule with
  `Decimal` and `ROUND_HALF_EVEN`; `bot/fees.py` is left as-is for the
  existing single-venue strategies (A/B), which don't need this precision at
  $10 notional, but should migrate to `bot/edge.py` if that ever changes.

## Kalshi API host

- Current recommended production REST host: `https://external-api.kalshi.com/trade-api/v2`.
- Demo: `https://external-api.demo.kalshi.co/trade-api/v2`.
- Older docs/guides reference `api.elections.kalshi.com` or
  `trading-api.kalshi.com` — both still resolve as of this writing but are
  not the currently-recommended host.
- **Discrepancy vs. existing code**: `bot/config.py` and `config.yaml` had
  `base_url` set to `api.elections.kalshi.com`. Fixed to
  `external-api.kalshi.com` in this session.
- Source: [docs.kalshi.com/getting_started/api_environments](https://docs.kalshi.com/getting_started/api_environments),
  corroborated via web search. Fetched 2026-08-09.
- Auth (for a future funded Kalshi leg, not yet implemented): RSA-PSS over
  `f"{timestamp_ms}{METHOD}{path}"` (path excludes query string), SHA-256,
  MGF1-SHA256, salt length = digest length. Headers `KALSHI-ACCESS-KEY`,
  `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`. Not re-verified
  against a live signed request in this session — the existing bot only uses
  Kalshi's public, unauthenticated orderbook endpoint.

## Venue scope

- Polymarket US (`polymarket.us`) confirmed as the only in-scope Polymarket
  venue for this deployment (US person, per `docs/DECISIONS.md`). Offshore
  Polymarket (`clob.polymarket.com`) remains permanently out of scope, per
  the existing `SPEC.md`.

## What Phase 0 did *not* verify (flagged, not fabricated)

- Kalshi's non-standard per-series fee multiplier table (see above).
- Live Kalshi RSA-PSS auth against a real signed request (no funded Kalshi
  leg exists yet — Kalshi is read-only/public in this codebase today).
- Polymarket US rate limits beyond the 60 req/min public discovery cap
  already encoded in `PolymarketFeedConfig.rest_rate_limit_per_sec`.
