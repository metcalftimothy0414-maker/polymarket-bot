# Build Prompt: Prediction Market Paper-Trading Harness (Phase 1) — FINAL

Paste everything below this line into Claude Code. Also save this file as `SPEC.md` in the repo root so future sessions can recover full context.

---

Build a Python paper-trading harness for Polymarket US that runs three strategies in parallel, simulates trades against the live order book, and logs everything per-strategy for edge comparison. **Phase 1 executes NO real orders** — it exists to determine which strategy (if any) has edge before capital is risked.

## Context (do not deviate)

- I am a US person. Polymarket US (the CFTC-regulated exchange) is the ONLY trading venue. Offshore Polymarket (clob.polymarket.com / Gamma / Polygon) is geoblocked for US persons and permanently out of scope — never suggest it, never call it.
- Kalshi: my account is active. Phase 1 uses Kalshi's PUBLIC market data only (no auth needed). Phase 2 (later, only if Strategy A survives) adds a funded Kalshi leg for true two-sided arb.
- Polymarket US currently lists sports (deepest liquidity) plus some politics/econ/weather. NO crypto Up/Down markets — do not build anything BTC-related.
- This machine is on a filtered/monitored network with a TLS-inspecting appliance. Gate all live data collection behind `data_collection_enabled: false` locally; full collection runs only after VPS deployment.
- Use Polymarket US's official Python SDK for all API access and auth — do not hand-roll request signing.

## Stack

- Python 3.11+, asyncio throughout
- Official Polymarket US SDK; `websockets`/`httpx` where needed beyond it
- SQLite for all logging (single file, no server)
- `pydantic` for config validation
- Deployable as a systemd service on a Linux VPS

## Architecture — 6 modules

### 1. `feeds/` — Data ingestion
- **Polymarket US**: WebSocket for live order book (best bid/offer + depth) on a configurable watchlist. REST only for market discovery/metadata on startup and hourly refresh, behind a token-bucket limiter (60 req/min public cap).
- **Kalshi (public, no auth)**: poll public market data endpoints for matching events. Default interval 15s.
- **The Odds API**: poll consensus sportsbook odds for matched games every 5 minutes (free tier: 500 req/month — budget the polling to stay under it).
- Auto-reconnect with exponential backoff; log every disconnect.

### 2. `matching/` — Event mapper (the hard part)
- Maps equivalent contracts across sources (Polymarket US ↔ Kalshi, Polymarket US ↔ sportsbook games).
- Match on normalized title tokens, resolution date, and category; require similarity above threshold AND matching resolution dates.
- **Critical**: store matches in a `pairs` table with `verified` defaulting to FALSE. Strategies act only on verified pairs. Provide `bot pairs review` — a CLI that shows both sides' full resolution criteria side by side for manual approval. Subtly different resolution rules are the #1 way divergence strategies lose money.

### 3. `strategies/` — Four modules behind a common interface

All implement `scan() -> list[Opportunity]` plus metadata (`strategy_id`, params hash). Every opportunity, candidate, and paper trade row is tagged with `strategy_id` so results never mix.

**Strategy A — `divergence/` (strategy_id: kalshi_divergence)**
- For each verified pair: compute mid-price divergence and, separately, executable divergence (Kalshi mid vs. Polymarket US best ask/bid — what could actually fill).
- Entry: executable divergence ≥ 4 cents after estimated fees (configurable).
- Record each opportunity with timestamp, both books' top 3 levels, divergence size, direction.
- Track persistence: sample until the divergence closes; record how long it lasted.

**Strategy B — `book_divergence/` (strategy_id: sportsbook_divergence)**
- Reference: consensus sportsbook odds from The Odds API for matched games (moneyline-equivalent contracts).
- Convert consensus odds to implied probability and REMOVE THE VIG (normalize both sides to sum to 1.0) before comparing. Raw odds overstate both sides by ~4-5% and manufacture fake divergence.
- Entry: de-vigged divergence vs. Polymarket US executable price ≥ 5 cents.
- Same opportunity/persistence logging as A.

**Strategy C — `momentum/` (strategy_id: sports_momentum)**
- Universe: same liquid sports contracts, live/in-progress events only.
- Signal: signed change in the contract's own mid-price over a lookback window (default 120s); enter in the direction of the move when |change| ≥ threshold (default 3 cents).
- Filters (all must pass): spread ≤ 3¢, implied prob 0.40–0.60 at entry, depth ≥ $500 on fill side.
- No LLM in the decision loop — signal computes in <100ms from cached WebSocket data.
- **Counterfactual logging (required)**: for every trade, record the mid-price 60s and 300s after entry, to measure whether momentum entries were systematically late.
- Skip-log every candidate (traded or not): implied prob, momentum value, spread, depth.
- Exit: hold to resolution, or early-exit if price reverses through entry by > 4 cents (configurable).

**Strategy D — `large_flow/` (strategy_id: large_flow)**
- Universe: same watchlist as Strategy C, driven by the public per-market trade feed (Polymarket US WS `SUBSCRIPTION_TYPE_TRADE`) rather than the order book alone.
- Signal: a trade whose size exceeds N times the market's rolling median trade size (default 10x, configurable via `size_multiple_threshold`); every print on this feed is taker-initiated (crossed the spread against a resting maker order by construction), so "aggressive" reduces to size, not a separate maker/taker classification. Requires a minimum trade-history sample before "median" is meaningful (5 trades) — a market's first few prints of the session don't get evaluated.
- Direction: enter the same direction as the large trade (buy if the aggressor bought, sell if the aggressor sold).
- Filters (all must pass, same as Strategy C): spread ≤ 3¢, implied prob 0.40–0.60 at entry, depth ≥ $500 on fill side.
- **Counterfactual logging (required)**: mid-price at +60s and +300s after every entry, same as C.
- Skip-log every candidate (traded or not): trade size, median multiple, spread, depth.
- Exit: hold to resolution or signal close (opportunity closes when no large-flow trigger persists), same open/close/persistence model as A/B/C.
- Kill criteria: same as the others, including the base-rate test (200+ resolved trades, win rate vs. average entry price).
- No REST equivalent of the trade feed exists — on the `rest_poll` transport fallback (blocked-WS networks), Strategy D simply never fires, same as running against a dead feed.

### 4. `paper/` — Simulated execution (shared)
- Simulate limit orders consistent with each strategy's signal.
- Fill simulation: an order "fills" only if the live book actually trades through the limit price within the timeout (A/B: 60s; C/D: 10s). No optimistic fills.
- Track entry price, $10 default notional, resolution outcome or signal-close exit.
- Apply the Polymarket US fee schedule to all simulated P&L.

### 5. `report/` — Metrics + logging
- Tables: `markets`, `pairs`, `opportunities`, `candidates`, `paper_trades`, `heartbeats`, `errors`.
- `bot report`: per strategy — opportunities, median persistence, simulated fill rate, win rate, average realized edge after fees, max drawdown, simulated P&L — plus a four-way comparison table.
- `bot export`: dump trade logs as CSV for weekly offline analysis.
- Heartbeat row every 5 minutes; structured file logging with rotation.

### 6. `dashboard/` — Localhost web UI
- FastAPI single-page dashboard at `http://localhost:8000`, live via `/ws/ui` WebSocket (no refresh).
- Mobile-first (viewed from a phone/iPad over Tailscale), scales to desktop.

**Page structure, top to bottom:**
1. **Header bar**: bot name in uppercase monospace, connection status dot (green pulsing = live, red = stale), mode badge ("PAPER" in amber — impossible to miss).
2. **Stats strip**: Simulated P&L (green/red), Win Rate, Open Positions, Opportunities Today. Strategy toggle ALL / DIV / BOOK / MOMO / FLOW filters every panel; per-strategy P&L shows side by side.
3. **Live markets panel**: watched pairs — market name, Polymarket price, reference price (Kalshi or de-vigged book), divergence in cents (highlighted green at threshold), 24h volume. Rows flash on update.
4. **Equity curve**: cumulative simulated P&L per strategy (uPlot or Chart.js, nothing heavy).
5. **Trade log feed**: reverse-chronological paper trades and opportunities, monospace timestamps, entry/exit, realized edge, strategy tag.
6. **System footer**: last heartbeat, WebSocket uptime, API error count, Odds API request budget remaining.

**Design spec (match exactly):**
- Background `#0A0A0C`, card surfaces `#111318`, 1px borders `#22262E`.
- Monospace throughout: `"JetBrains Mono", "SF Mono", monospace`. Labels uppercase, letter-spaced, 10–11px, muted gray `#6B7280`.
- Positive `#22C55E`, negative `#EF4444`, warning/paper-mode `#F59E0B`, neutral text `#D1D5DB`.
- Corners ≤ 4px, no shadows, no gradients. Density over whitespace — terminal, not marketing page.
- Numbers are the interface: prices and P&L largest, labels small and quiet beneath.
- Signature detail: divergence renders as a horizontal bar meter that fills toward the entry threshold — readable across a room.
- Respect `prefers-reduced-motion`.

## Config (`config.yaml`)

```yaml
data_collection_enabled: false   # flip to true on the VPS only
strategies:
  kalshi_divergence:
    enabled: true
    watchlist_categories: ["nba", "mlb"]
    entry_threshold_cents: 4
    fill_timeout_seconds: 60
    kalshi_poll_seconds: 15
  sportsbook_divergence:
    enabled: true
    entry_threshold_cents: 5
    odds_poll_minutes: 5
    fill_timeout_seconds: 60
  sports_momentum:
    enabled: true
    momentum_lookback_seconds: 120
    momentum_threshold_cents: 3
    exit_reversal_cents: 4
    fill_timeout_seconds: 10
position_notional_usd: 10
max_concurrent_positions: 5      # global
daily_sim_loss_stop_usd: 50      # per strategy — one dying doesn't halt the others
```

## Secrets (`.env`, never committed; `.env.example` with empty values)

```
POLYMARKET_API_KEY_ID=
POLYMARKET_PRIVATE_KEY=
ODDS_API_KEY=
```

Fail fast on startup with a clear error if any required key is missing.

## Risk rules (hardcoded, not configurable above these caps)

- No live-execution code path exists in Phase 1. None.
- Halt a strategy if its data is stale: >30s for A/B, >5s for C.
- Skip any market with <$500 visible depth on the fill side.

## Kill criteria (in the report, evaluated per strategy)

Flag a strategy DEAD after 100+ opportunities if:
- Simulated fill rate < 30%, OR
- Average realized edge after fees < 1 cent, OR
- Median opportunity persistence < 10 seconds.

For B and C additionally, after 200+ resolved trades: realized win rate must beat the average entry price (winning 55% of trades entered at 55¢ is zero edge). The report compares win rate against average entry price, never against 50%.

## Deliverables

1. Full repo with the 6 modules; tests for the matcher, vig removal, and fill simulator
2. `README.md` with VPS deployment steps (systemd unit included) and Tailscale dashboard access notes
3. CLI: `bot run`, `bot pairs review`, `bot report`, `bot export`
4. `SPEC.md` = this document, in the repo root
5. `.env.example`

## Build order

1. Feeds + market discovery; show me live Polymarket US data before anything else (or mock data if data_collection is disabled locally)
2. Matcher + `bot pairs review`
3. Strategy interface + all three strategy modules
4. Paper execution + fill simulation
5. Reporting with the three-way comparison
6. Dashboard last, once real data flows into SQLite

Start with step 1 and confirm data flows before building further.

---

## Reconciliation notes (added post-hoc, not part of the original prompt)

This spec was pasted into an existing, already-built repo (all 6 modules, 91+ tests,
CLI, dashboard already implemented across prior sessions). Rather than rebuild, the
repo was diffed against this doc and reconciled:

- **Official SDK**: confirmed `polymarket-us` on PyPI (github.com/Polymarket/polymarket-us-python,
  verified org, "Official Polymarket US Python SDK") is genuine — its `create_auth_headers`
  implements the exact same Ed25519 scheme this repo had hand-rolled and validated against
  live traffic. Swapped `bot/feeds/auth.py` to delegate to it. Requires Python 3.11+, which
  this dev machine didn't have (was on system Python 3.9) — added a `.venv` on Homebrew
  python@3.11 so the SDK is actually installable and tested here, not just declared in
  requirements.txt.
- **Strategy A naming**: this doc names it `kalshi_divergence`; the existing code had it as
  a bare `divergence`, ambiguous against `sportsbook_divergence`. Renamed for clarity
  (config key, STRATEGY_ID constant, existing DB rows migrated — see git log).
- Offshore Polymarket, Kalshi-blocked-on-this-network, BTC-market exclusion, and the
  `data_collection_enabled` gate were all already correctly in place from earlier sessions.
- The 6-module architecture in this doc maps to `bot/feeds`, `bot/matching`, `bot/strategies`,
  `bot/paper.py`, `bot/report.py`, `bot/dashboard.py` — kept as one `bot/` package rather than
  6 top-level dirs, since that's what already existed and there was no functional reason to
  split it.

## Watchlist / league filtering fix (post-hoc)

Discovery was only ever surfacing UFC markets in practice. Root cause, confirmed against
live data: every sports market's `category` field is literally always `"sports"` — it
never carries `mlb`/`nba`/`ufc`/etc, so `feeds.polymarket.categories` was never able to
filter by league (`categories=mlb` returns 0 results; `sport=`/`league=`/`leagues=` query
params are silently no-ops). The only field present on every market — team-based and
individual sports alike — that reliably encodes the league is the slug's 2nd
hyphen-segment (`tec-mlb-...` → `mlb`). Added `feeds.polymarket.leagues` as a client-side
filter (`bot.feeds.polymarket.filter_by_leagues`) applied after discovery. `config.yaml`
now sets `["mlb", "f", "fwc"]` — MLB (in season) plus FIFA World Cup (in progress,
currently the single highest-24h-volume league on the exchange) — `nba` excluded
(offseason until October).

## Strategy D (post-hoc)

Added `large_flow` per an updated spec. Uses the Polymarket US WS's
`SUBSCRIPTION_TYPE_TRADE` channel (discovered live — not in the REST API surface at all;
`PolymarketRestPoller`'s `.trades` is always empty, so Strategy D is inert on the
`rest_poll` transport fallback). Every print on that feed is taker-initiated by
construction, so "aggressive trade that crosses the spread" reduces to a size check
against a rolling per-market median (min 5 samples before a multiple means anything).
`bot/report.py`'s base-rate test was previously only ever wired up for `sports_momentum`
despite the original spec saying "B and C" — generalized it to run for
`sportsbook_divergence`, `sports_momentum`, and `large_flow` alike (not
`kalshi_divergence`, whose edge is priced directly cross-venue in cents). Also fixed a
stale `divergence` key (pre-rename) in the dashboard's `STRATEGY_LABELS` JS map, found
while adding the `FLOW` toggle next to it.
