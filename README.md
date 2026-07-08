# polymarket-bot

Phase 1 paper-trading harness for Polymarket US. Runs four strategies in
parallel against live order books, simulates fills (no real orders — there
is no live-execution code path in this build), and logs everything
per-strategy so the strategies can be compared and killed on evidence
rather than vibes.

**Strategy A — `kalshi_divergence`**: Kalshi mid-price vs. Polymarket US executable
price on manually-verified cross-venue market pairs.
**Strategy B — `sportsbook_divergence`**: de-vigged consensus odds from The
Odds API vs. Polymarket US price, same verified-pair discipline.
**Strategy C — `sports_momentum`**: short-window momentum on the Polymarket
contract's own price, single-venue, mechanically filtered.
**Strategy D — `large_flow`**: follows abnormally large trades (10x+ the
market's rolling median trade size, configurable) off the public trade feed,
same mechanical filters as C.

## Architecture

```
bot/
  feeds/          Polymarket US (REST + authenticated WS), Kalshi (poll), Odds API (poll)
  matching/        Cross-venue pair matcher + bot pairs-review / odds-pairs-review CLIs
  strategies/      Strategy interface + A / B / C, all sharing an opportunity lifecycle
  paper.py         Fill simulation, position P&L, resolution settlement
  report.py        Per-strategy metrics, kill criteria, bot report / bot export
  dashboard.py      FastAPI + WebSocket live UI (single page)
  runner.py        Wires feeds + strategies + paper together behind data_collection_enabled
```

SQLite (`data/bot.db`) is the only datastore: `markets`, `pairs`,
`odds_pairs`, `opportunities`, `candidates`, `paper_trades`,
`market_snapshots`, `heartbeats`, `errors`.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in POLYMARKET_API_KEY_ID, POLYMARKET_PRIVATE_KEY, ODDS_API_KEY
```

Credentials come from the Polymarket US developer portal
(polymarket.us/developer) — an Ed25519 key pair, base64-encoded secret.
The Odds API key comes from the-odds-api.com. Never commit `.env`
(already gitignored).

**`data_collection_enabled` in `config.yaml` defaults to `false`.** No feed
makes a live network call while it's false — this is deliberate: keep it
false on a dev machine, set it `true` only on the VPS. `bot feeds-check
--allow-live` and `bot pairs-scan --allow-live` (etc.) override the gate
for a one-off manual check without changing the committed default.

## CLI

```
bot feeds-check [--seconds N] [--allow-live]     # confirm live Polymarket data flows
bot pairs-scan --polymarket-category X --kalshi-series Y [--allow-live]
bot pairs-review                                  # approve/reject proposed Kalshi pairs
bot odds-pairs-scan --polymarket-category X --odds-sport-key Y [--allow-live]
bot odds-pairs-review                             # approve/reject proposed Odds API pairs
bot run                                           # live scan + paper-trade loop
bot report                                        # per-strategy metrics, kill criteria, comparison
bot export [--out FILE]                           # paper_trades table as CSV
bot dashboard [--host H] [--port P] [--ngrok]     # localhost dashboard, optional ngrok tunnel
```

**A pair is never traded until reviewed.** `pairs`/`odds_pairs` rows default
`verified = 0`; only `bot pairs-review` / `bot odds-pairs-review` can flip
that. Subtly different resolution rules between venues is the single
biggest way a cross-venue strategy loses money — a false match looks like
free money and is actually a coin flip.

## Risk rules (hardcoded, not config)

- No live orders anywhere in this codebase.
- Scanner skips any market with order-book data older than 30s (Kalshi/Odds
  API: 60s, since they're polled not streamed).
- Skip any market with <$500 visible book depth on the fill side (Strategy C).

## Running tests

```bash
python -m unittest discover -s tests
```

## VPS deployment

```bash
sudo useradd -r -m -d /opt/polymarket-bot polymarket-bot
sudo -u polymarket-bot git clone <this repo> /opt/polymarket-bot
cd /opt/polymarket-bot
sudo -u polymarket-bot python3.11 -m venv .venv
sudo -u polymarket-bot .venv/bin/pip install -r requirements.txt
sudo -u polymarket-bot cp .env.example .env   # fill in real credentials
```

Edit `config.yaml` and set `data_collection_enabled: true` — this should
only ever be `true` on the VPS, never on a dev machine.

```bash
sudo cp deploy/polymarket-bot.service /etc/systemd/system/
sudo cp deploy/polymarket-bot-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot polymarket-bot-dashboard
sudo journalctl -u polymarket-bot -f
```

The dashboard unit binds to `127.0.0.1:8000` deliberately — it has no
authentication, so don't expose it directly to the public internet.
Reach it either over Tailscale (connect to the box, browse to
`http://<tailscale-ip>:8000`) or with `bot dashboard --ngrok` run
manually for a one-off remote-viewing session (requires an ngrok
authtoken already configured via `ngrok config add-authtoken`).

## Known gaps / next steps

- **Polymarket US WS auth**: implemented exactly per
  `docs.polymarket.us/api-reference/authentication` (Ed25519, verified
  byte-for-byte against the docs) and confirmed structurally correct
  (REST discovery/book work live), but WS signature auth has not been
  verified live from this dev environment — the local network strips or
  mangles the custom `X-PM-*` auth headers. Smoke-test `bot feeds-check
  --allow-live` from the VPS before relying on it.
- **Kalshi**: client code is written to the documented v2 REST shape but
  has not been exercised live — this dev network has a TLS-intercepting
  proxy on Kalshi's domain. Smoke-test from the VPS.
- **Resolution settlement** reads the final payout straight off a closed
  Polymarket market's `marketSides[].price` (confirmed live against the
  real API) — no separate resolution feed exists or is needed.
