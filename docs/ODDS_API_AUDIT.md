# Odds API Quota Audit (2026-08-09)

## Finding: quota is fully exhausted

Verified live against the account's own API responses (the response
headers on every call mirror the account dashboard's usage numbers —
these are the same underlying counter):

```
GET /v4/sports/  (free endpoint, doesn't consume credits)
HTTP/2 200
x-requests-remaining: 0
x-requests-last: 0
x-requests-used: 500

GET /v4/sports/baseball_mlb/odds/?regions=us&markets=h2h  (paid endpoint)
HTTP/2 401
x-requests-remaining: 0
x-requests-used: 500
{"message":"Usage quota has been reached. See usage plans at https://the-odds-api.com",
 "error_code":"OUT_OF_USAGE_CREDITS", ...}
```

**500/500 credits used, 0 remaining.** This confirms the task's suspicion
exactly: every `/odds` call has been returning 401
`OUT_OF_USAGE_CREDITS` since the quota ran out, which the existing code
logs as a generic `httpx.HTTPError` warning — indistinguishable on the
dashboard from "feed healthy, no opportunities found." `sportsbook_
divergence` has been silently dead, not silently idle.

I don't have interactive login to the account dashboard at
the-odds-api.com to read the exact reset date — the API response headers
above are the same live counter the dashboard displays, but they don't
expose the billing-period reset timestamp. Check the dashboard directly
for that if it matters (free-tier periods are calendar-month, per
the-odds-api.com's stated plan terms).

## Current configuration and actual burn rate

From `config.yaml` (before this task) and `bot/feeds/odds_api.py`:

| Parameter | Value | Source |
|---|---|---|
| `poll_seconds` | **30** | `config.yaml` |
| `markets` | `h2h` (1 market) | hardcoded default in `OddsApiFeedClient.get_odds()` — not actually configurable before this task |
| `regions` | `us` (1 region) | `config.yaml` |
| verified sport_keys | **2** (`mma_mixed_martial_arts`, `baseball_mlb`) | `SELECT DISTINCT odds_api_sport_key FROM odds_pairs WHERE verified=1` |

Cost per `/odds` call = `markets × regions` = 1 × 1 = **1 credit**.
`OddsApiFeedClient.poll()` calls `/odds` once per verified sport_key per
cycle, so:

```
calls/cycle = 2 sport_keys
cycles/day  = 86400 / 30 = 2,880
calls/day   = 2 × 2,880 = 5,760
credits/day = 5,760 × 1 = 5,760
```

Against a 500-credit **monthly** budget, that's enough to exhaust the
entire month's quota in **500 / 5,760 ≈ 0.09 days ≈ 2 hours** of the bot
running with `data_collection_enabled: true`. This is not a marginal
overage — the configured cadence was ~345x the sustainable rate for two
tracked sport keys, let alone any more that get verified later.

## Fix and projected burn under the new default

New defaults (this task, §4): `poll_interval_seconds: 14400` (4h),
`markets: ["h2h"]`, `regions: ["us"]` (both already minimal — no `spreads`/
`totals` were ever being requested, contrary to the task's framing that
they needed dropping; the real problem was purely the interval).

```
calls/cycle = 2 sport_keys (current verified count)
cycles/day  = 86400 / 14400 = 6
calls/day   = 2 × 6 = 12
credits/day = 12 × 1 = 12
credits/month (30d) = 360
```

**360/month against the 400 configured budget — under budget, with
~40 credits of headroom.** This is higher than the task's own illustrative
"6 calls/day ≈ 180 credits/month" figure because that number implicitly
assumes a single sport_key; the real count is 2 today and will grow as
more sportsbook pairs get verified. That's exactly why §4 also adds a
hard client-side `monthly_credit_budget` circuit breaker — the interval
alone doesn't bound spend as the verified-pair count changes, only the
budget check does.

## What changed to prevent recurrence

- `feeds.odds_api.markets`/`regions` are now real config fields (list),
  not a hardcoded default — see `config.yaml`.
- A monthly credit counter persisted in SQLite (`odds_api_usage` table)
  stops calling `/odds` entirely once `monthly_credit_budget` is reached,
  rather than relying on the API to reject the call (§4).
- `x-requests-remaining`/`x-requests-used` are read from every response
  (success or error) and persisted, surfaced on the dashboard as
  `odds_api_credits_remaining`, with an alert threshold at 20% remaining
  (§2).
- A 401 with `error_code: OUT_OF_USAGE_CREDITS` (or any feed error) now
  sets that feed's status to `DEGRADED`, distinct from `IDLE` — see
  `docs/CATEGORY_EXPANSION.md`-style feed health tracking added in §2.
