# Category Expansion (2026-08-09)

Extends `SPEC.md`'s sports-only scope to the full Kalshi/Polymarket US
market universe, in observe-only mode by default. This document records
what was verified against primary sources, what was assumed, and what
deliberately deviates from the original task spec (with the reason why).

## What was verified live, and when

All verified 2026-08-09 against the running APIs (not docs alone):

- **Kalshi `/series` returns `fee_multiplier` and `fee_type` directly**,
  covering all ~12,575 series in one unpaginated call. `fee_multiplier`
  IS `M_taker`; `M_maker` equals it only when `fee_type` ends
  `"_with_maker_fees"`. This **contradicts** the task's own claim that
  "all sports series are 1/1" — confirmed live: `KXMLBGAME` is `0.5/0.5`
  (`quadratic_with_maker_fees`), `KXNFLPASSYDS` (Sports category) is
  `1/0` (plain `quadratic`, no maker fee at all). Every ticker in the
  task's given 0/0 list (`KXBTCY`, `KXETHY`, `KXGREENLAND`, `KXDOED`,
  `KXLAYOFFSYINFO`, `KXCITRINI`, `KXELECTIRAN`, `KXIRANDEMOCRACY`,
  `KXPAHLAVIHEAD`, `KXGAMBLINGREPEAL`) confirmed `fee_multiplier: 0`
  live. The live field is authoritative and is what the fee engine
  actually uses — the hardcoded list and the "sports = 1/1" rule were
  both dropped in favor of it (§1, `bot/fee_multipliers.py`).
- **Real universe scale**: 766,612 open Kalshi markets across ~12,575
  series (one full `/markets?status=open` enumeration, 767 pages at
  limit=1000, ~17 minutes). 15,243 open Polymarket US markets across 10
  real categories: `sports` (14,109), `politics` (279), `culture` (498),
  `finance` (73), `geopolitics` (9), `technology` (68), `macro` (83),
  `crypto` (50), `science` (14), `climate` (60) — found by calling
  `/v1/markets` with no `categories` filter at all, rather than guessing
  category strings one at a time.
- **Kalshi Basic tier rate limit**: ~20 read req/s (200 read tokens/s ÷
  10 tokens/request) ≈ 1,200 req/min.
- **Polymarket US rate limit**: 20 req/s per API key
  (docs.polymarket.us/api-reference/rate-limits) ≈ 1,200 req/min —
  matches the pre-existing `rest_rate_limit_per_sec: 20` config default.

## What was assumed (not independently verified)

- **Kalshi series tickers never contain a hyphen.** Used to derive
  `series_ticker` from `event_ticker`/`market_ticker` by splitting on the
  first `-`. Held for every ticker observed this session (hundreds,
  across a dozen+ series families) but is a pattern inference, not a
  documented guarantee.
- **Named-source / comparison-operator / reference-period extraction**
  (`bot/matching/normalizers.py`) uses regex heuristics, not an NLP
  pipeline. These feed the divergence tier (§4), not the match score
  itself — a missed extraction raises the tier (more conservative), it
  never silently approves something.
- **Divergence tier thresholds** (discretionary language, missing
  timezone, missing cancellation handling, etc.) are the task's own
  named signals, implemented directly. Their *severities* (which signal
  bumps tier by how much) are a first pass, not calibrated against real
  settlement outcomes — there's no settlement history for non-sports
  categories yet to calibrate against. Recalibrate once §7's persistence
  data and real settlements accumulate.

## Deliberate deviations from the task spec, and why

1. **Tier C is not on a literal 10-minute cadence.** At the confirmed
   scale (766,612 Kalshi markets), a *full* catalog re-enumeration takes
   ~17 minutes for Kalshi alone — a 10-minute cycle would overlap itself.
   `catalog_refresh_loop` runs hourly (unchanged from before this task).
   This is the "cap the universe by liquidity rather than silently
   starving the polling loop" case the task anticipates, except the honest
   answer at this stage is "the metadata-only catalog refresh itself
   can't hit 10 minutes at this scale, so it doesn't try to" — Tier
   B/A book-level polling (the part that actually matters for catching
   live divergence) is unaffected and does run at the task's specified
   cadences.
2. **Tier B promotion is match-time-tier-based, not live-edge-based.**
   The task frames Tier B as "candidate pairs, tier 1-3, passing a coarse
   pre-filter." Tier C markets have no book access (metadata-only by
   definition), so they structurally cannot be promoted based on
   *observed* live edge — that would be circular. The "coarse pre-filter"
   is §4's tier assignment itself, computed once at match time from the
   resolution-text signals. Demotion (Tier B → C for a pair with no
   positive edge in its last 20 evaluations) *is* live-observation-based,
   since Tier B pairs do have book access.
3. **`kalshi_divergence`/`sportsbook_divergence` were not touched.**
   Both already iterate `pairs WHERE verified = 1` (or the `odds_pairs`
   equivalent) with no sports-specific filtering in the query itself —
   they are already category-agnostic in the sense the task asks for.
   Their edge model (single-leg entry on a cross-venue price signal, no
   capital-lockup/annualization) doesn't fit §5's locked-pair scoring
   formula, which is why all of §5's new machinery
   (`gross_edge`/`fee_cost`/`net_edge`/`annual_return`/`binding_constraint`)
   landed in `locked_pair_arb` only.

## Verified pairs untouched

No existing verified pair, sports or otherwise, was modified by this
work. `pairs.category` defaults to `'sports'` on the schema migration, so
every pre-existing row keeps behaving exactly as it did before. §3's
regression test (`tests/test_matcher.py::TestCategoryExpansionRegression`)
asserts byte-identical output between `find_candidate_pairs` and
`find_candidate_pairs_by_category("sports", ...)` on the same input, not
just equal counts.
