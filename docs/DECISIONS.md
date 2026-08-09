# Decisions

Answers to the cross-venue arbitrage build prompt's §13 questions, as given
(or defaulted, where noted) in the 2026-08-09 session that extended this
repo toward Type 3 locked-pair arbitrage.

1. **Polymarket venue / jurisdiction**: Polymarket US (`polymarket.us`).
   Operator is a US person. Offshore Polymarket is out of scope (already
   established in `SPEC.md` prior to this session).

2. **Total capital / venue split**: not yet specified by the operator. All
   sizing config (`position_notional_usd`, future `max_notional_per_pair`,
   etc.) ships with small placeholder defaults ($10) and must be set to real
   values before any live trading is considered — which is gated far behind
   this session's work regardless (see §8 of the build prompt; this repo has
   no live-execution code path at all).

3. **Language/stack**: Python (existing repo, asyncio, SQLite, pydantic) —
   matches the build prompt's own recommendation. No reason to deviate.

4. **Where it runs**: existing repo already documents a systemd VPS
   deployment (`README.md`); unchanged by this session.

5. **Market category scope**: unchanged from the existing repo — Polymarket
   US sports markets (MLB, FIFA World Cup) via `config.yaml`'s
   `feeds.polymarket.leagues`. The new locked-pair strategy reuses whatever
   pairs are already verified via `bot pairs-review`; it does not widen
   category scope on its own.

6. **Risk tolerance for a naked leg**: not explicitly set by the operator
   this session. Defaulted conservatively: the new `locked_pair_arb`
   strategy only *opens* a two-leg paper position when both legs are
   simultaneously simulated as filled (Mode A / simultaneous-taker
   equivalent per the build prompt's execution taxonomy) — it does not rest
   a maker order and hope to hedge later. This sidesteps hedge-fail risk
   entirely in this phase, at the cost of being the lower-EV execution mode
   (see build prompt §3.5). Revisit when/if Mode B is built.

7. **Operator availability**: not specified. No auto-halt/paging
   infrastructure exists yet (out of scope for this session — see "Not
   built this session" below).

8. **Tax/record-keeping**: not specified. Existing `paper_trades`/new
   `pair_positions` tables are append-only-ish SQLite rows with entry/exit
   prices and fees, sufficient for later CSV export (`bot export`); no
   per-lot/1099 structure requested or built.

9. **Existing code to integrate**: yes — this repo itself. Discovered
   mid-session (the operator's initial answer was "starting from scratch,"
   before this repo was found). Decision: **extend polymarket-bot** rather
   than build a parallel system, reusing its feeds, pairing/review gate,
   paper execution, and reporting infrastructure.

10. **Return threshold hurdle**: not specified. `bot/edge.py` defaults
    `hurdle_annual_return` to 25%, matching the build prompt's own default,
    exposed as a parameter so it can be overridden per call/config later.

## Strategy scope decision

The build prompt's Type 5 (directional/model-driven trading) is explicitly
out of scope and "if the agent proposes it, refuse." The pre-existing
`sports_momentum` and `large_flow` strategies fall in that category (signals
are the market's own price momentum / large-trade following, not a
cross-venue locked pair) and were removed in this session rather than kept
alongside the new arb strategy. `kalshi_divergence` and
`sportsbook_divergence` were kept — they use cross-venue/cross-source
information as a directional entry signal on a *single* venue (no locked
pair, no hedge), which is a gray area versus the new taxonomy, but the
operator did not ask for their removal and they predate this build prompt.

## What this session built vs. what remains

**Built**: fee-and-risk-adjusted edge math (`bot/edge.py`) implementing the
build prompt's §3 fee formulas (Decimal, correct rounding per venue) and §5
edge/book-walking/risk-adjustment math; a new `locked_pair_arb` strategy
(Type 3) that only enters a genuinely locked two-leg position (buy YES on
one venue + NO on the other) when the risk-adjusted edge clears a
configurable hurdle, and holds to settlement rather than exiting early;
`pair_positions` and `settlements` tables to track leg-level state and
predicted-vs-realized divergence outcomes, per the build prompt's §9 schema.

**Not built this session** (explicitly deferred, not silently skipped):
live order execution of any kind (this repo has never had one and still
doesn't); the Risk Manager module's full veto list (§7.6); Treasury/
Rebalancer (§7.7); Prometheus metrics/alerting (§7.9); Postgres migration
(still SQLite, adequate at this scale); Kalshi authenticated trading (feed
is still public/read-only); on-chain Type 1/2 intra-venue arbitrage; the
60-day paper-trading gate itself (§8) — that requires real elapsed time
running the bot, not something a single session can complete; dashboard
(`bot/dashboard.py`/`bot/static/index.html`) integration for
`locked_pair_arb` — its data model (two-leg `pair_positions`/`settlements`)
doesn't fit the existing single-leg `paper_trades`-shaped panels, so it's
currently only visible via `bot report`, not the live UI.
