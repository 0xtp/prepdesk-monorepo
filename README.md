# PerpDesk

A self-hosted crypto **perpetual-futures signal desk**. For every coin you follow it produces a concrete, deterministic trade plan:

- direction, always **LONG** or **SHORT**, with a grade (**A** / **B** / **C**) and any **⚠ caution flags**;
- entry, stop loss, take-profits, size, leverage and estimated liquidation;
- win probability, break-even rate and expected value.

Every plan is logged and scored against what price actually did. You place trades by hand on Hyperliquid or in Phantom. Automated execution is Phase 2, designed in [`docs/PHASE2.md`](docs/PHASE2.md) but not built.

> Signals are probabilistic. Not financial advice. The Track Record page shows honestly whether they work.

- **Data:** Hyperliquid (primary), DexScreener and GeckoTerminal (tokens without a perp), CoinGecko (search), alternative.me Fear & Greed, Yahoo (CME BTC futures, DXY, S&P 500), the ForexFactory calendar, and Coinglass (optional). Every number in the app comes from these live APIs. Mocks exist only in `tests/`.
- **Stack:** Python 3.12 · FastAPI · Jinja2 + HTMX + Alpine.js · Tailwind (pre-built) · TradingView Lightweight Charts · SQLAlchemy 2 + Alembic · Postgres (Supabase/Neon) or SQLite · Vercel.

---

## Contents
1. [Deploy on Vercel (step by step)](#1-deploy-on-vercel)
2. [Scheduling: why and how](#2-scheduling)
3. [Run the backtest](#3-backtest)
4. [How to read a plan](#4-how-to-read-a-plan)
5. [Grades, gates and the Track Record](#5-grades-gates-and-the-track-record)
6. [Configuration reference](#6-configuration) (includes My trades and live prices)
7. [Local development](#7-local-development)
8. [Architecture](#8-architecture)
9. [Phase 2 outline](#9-phase-2-outline)
10. [Limitations](#10-limitations)

---

## 1. Deploy on Vercel

You need a GitHub account (this repo), a Vercel account (Hobby is fine) and a Postgres database. Supabase's free tier works well. Neon from the Vercel Marketplace also works.

1. **Database (Supabase).** In Supabase: *Project → Connect → Connection string → Transaction pooler*. Copy the URI. It looks like `postgresql://postgres.xxxx:PASSWORD@aws-0-...pooler.supabase.com:6543/postgres`. Use port **6543**, the pooler, because serverless functions open short connections. The app turns off prepared statements for the pooler and creates its tables on first request (Alembic migrations).
2. **Import the repo into Vercel.** *Add New → Project → Import* `prepdesk-monorepo`. Framework preset: **Other**. Leave the build settings empty: `vercel.json` and `requirements.txt` are already set up, and the CSS is pre-built.
3. **Environment variables** (Vercel → Project → Settings → Environment Variables). Required:
   - `DATABASE_URL` = the Supabase URI from step 1
   - `CRON_SECRET` = a long random string, e.g. from `openssl rand -hex 24`

   Optional: `ANTHROPIC_API_KEY`, `COINGECKO_API_KEY`, `AUTH_ENABLED=true` with `UI_PASSWORD_HASH` and `SESSION_SECRET`, Telegram keys. Every variable is listed in [`.env.example`](.env.example).
4. **Deploy.** Open the URL. The first scheduled run (or pressing **Refresh all**) seeds your default favourites and analyses them.
5. **Turn on scheduling** (next section) so plans are logged and scored even while you are away.
6. **Optional password:** generate a hash with `python -c "import bcrypt;print(bcrypt.hashpw(b'your-password',bcrypt.gensalt()).decode())"`, set `AUTH_ENABLED=true`, `UI_PASSWORD_HASH=<hash>` and `SESSION_SECRET=<random>`, then redeploy.

Health check: `GET /healthz` returns the DB status and the age of the last scheduled run. The header shows a "● tick N min ago" heartbeat.

## 2. Scheduling

**Why it is needed:** the app has to be honest about whether its signals work. That means generating and scoring plans on a fixed schedule, not only when you happen to open the page. Without a schedule:

- the track record would only contain moments you looked (selection bias);
- the statistical gates and calibration would never get enough live data;
- Telegram alerts could not fire.

Vercel's own cron runs **at most once a day on the Hobby plan**. That is used for the daily maintenance job only (`vercel.json`). For the 10-minute **tick**, pick one:

| Option | Cost | Setup |
|---|---|---|
| **GitHub Actions** (included: `.github/workflows/tick.yml`) | free | Repo → Settings → Secrets and variables → Actions → add `APP_URL` (your Vercel URL) and `CRON_SECRET` (same value as in Vercel). It runs every 10 minutes from the default branch; GitHub may delay runs by a few minutes. |
| Vercel Pro | $20/month | Add `{"path": "/api/cron/tick", "schedule": "*/5 * * * *"}` to `crons` in `vercel.json`. Vercel sends the `CRON_SECRET` automatically. |
| Any free pinger (cron-job.org etc.) | free | `GET https://<app>/api/cron/tick` with header `Authorization: Bearer <CRON_SECRET>` every 5–10 min. |

Irregular ticks are fine. The scorer re-reads Hyperliquid's 1-minute candles (about 3.5 days are kept) from each plan's creation time, so a late tick loses no accuracy.

## 3. Backtest

GitHub → Actions → **backtest** → *Run workflow* (coins, days, ablation). It downloads history and runs the walk-forward backtest. It uploads an HTML report as an artifact and, if the repository secret `DATABASE_URL` is set, saves the results to the app (**Backtests** page). Only then can a setup earn grade A and plans show win probabilities. A weekly scheduled run keeps the statistics current.

- **History:** Binance USDT-M futures archive (`data.binance.vision`) for anything older than Hyperliquid's ~5,000-bar window, Hyperliquid candles inside it, and Hyperliquid funding throughout.
- **Locally:** `perpdesk backtest --coins SOL,BTC --days 730 --ablation [--db]`.
- The report states its sample sizes and limitations, and says when a sample is too small to conclude anything.

## 4. How to read a plan

```
SOL-PERP SHORT · Grade C ⚠
Entry: limit 106.34–106.59
SL: 107.77  TP1: 105.26 (50%)  TP2: 103.22 (50%)  → SL to breakeven after TP1
Size: 1.77 SOL · 1x isolated · margin $188.43
Risk $2.49 · R:R 1.62 to TP1 after fees
Est. liq 207.7
Invalid if: 1H close above 107.383 (resistance ×3 (4H)) · unfilled after 60 min or price beyond 105.16
```

- **Direction + grade:** always LONG or SHORT. The grade says how much to trust the call and scales the size: A = full risk, B = half, C = quarter. **⚠** means at least one caution flag applies (event soon, extreme volatility, stale data, low liquidity, BTC counter-trend, crowded funding, R:R below minimum). Flags reduce size further; hover the ⚠ to see why.
- **TAKE / SKIP:** TAKE if the grade is A, or if that grade's *live* expectancy is positive over at least `MIN_SAMPLE_FOR_STATS` trades. Otherwise SKIP. SKIP calls are shown, not hidden.
- **Entry:** the preferred limit zone is the nearest sensible pullback level (VWAP, EMA, reclaimed level, fair value gap). The market price is the alternative, and the plan says which to use.
- **Stop:** beyond the nearest invalidating structure plus an ATR buffer, never closer than `MIN_SL_ATR` × ATR.
- **Targets:** TP1 at the first significant opposing level or 1.5R, whichever is more conservative; TP2 at the next level, 2.5–3R. Take 50% at TP1 and move the stop to breakeven.
- **Size:** `quantity = risk $ ÷ loss per coin at the stop` (fees, slippage and funding included). Leverage is an *output*: the smallest whole number that fits the notional within your margin, capped by `MAX_LEVERAGE`, the asset's Hyperliquid maximum and the liquidation check. If a cap binds, the size shrinks; the risk never grows.
- **Leverage slider** (asset page, from 2x up to the asset's Hyperliquid maximum):
  - **What changes:** at your margin, size = margin × leverage. The recommended leverage, marked ▲, is the plan as designed. Lower leverage keeps the plan's stop and targets at a smaller size.
  - **Above the recommended leverage:** the stop is tightened so the loss still equals your risk budget, and TP1/TP2 scale to keep the same reward-to-risk. The panel always shows what keeping the plan's stop would cost at that leverage.
  - **Colours:** green = the plan; amber = tighter stop and targets than the tested plan; red = the stop is inside normal noise (under `MIN_SL_ATR` × ATR), liquidation comes before the stop, or no stop can hold the risk once fees are counted.
  - **Calculation:** every step is precomputed on the server with the same liquidation and fee maths, so dragging is instant.
- **Liquidation:** isolated margin with Hyperliquid's margin tiers (maintenance = half the initial margin at max leverage). The stop must sit at least `MIN_LIQ_BUFFER_ATR` × ATR before liquidation, or leverage is lowered and a flag added.
- **R:R** is after fees and expected funding. **Win %** is P(TP1 before SL) from out-of-sample backtests of the same setup × asset × direction (falling back to asset class, then all assets), calibrated on the confidence score, with a 95% range and sample size. "~ low sample" means too few trades. **Break-even** is the win rate needed at this R:R. **EV** is the expected $ per trade.
- **Copy plan** copies the compact block above for Hyperliquid or Phantom. **I took this** records your real fill so your own trades are tracked separately.
- **Spot view only:** tokens without a Hyperliquid perp (most contract addresses) are analysed from DEX data, labelled, and never given leverage.

## 5. Grades, gates and the Track Record

- **Setups:** trend pullback, range-edge reclaim, breakout with volume, funding-crowded fade, plus "composite bias" when none fires. Each plan names its setup.
- **A requires every gate**, measured out-of-sample with walk-forward validation:
  - ≥ `GATE_MIN_TRADES` trades;
  - expectancy > `GATE_MIN_EXPECTANCY_R` after costs;
  - Wilson-95% win-rate lower bound above break-even;
  - profit factor > `GATE_MIN_PF`;
  - drawdown < `GATE_MAX_DD_R`;
  - positive in 3 of the last 4 quarters.

  Otherwise a call is B (confidence ≥ `MIN_CONFIDENCE`) or C. Composite bias can never be A.
- **Live gate:** an A setup whose live expectancy turns negative, or whose last 30 trades fall below the backtest's 5th percentile, is demoted to B automatically (daily job). It is promoted back only by passing the gates again in a new backtest.
- **Expect few or no A grades at first.** Until a backtest shows a setup passes every gate, all calls are B or C. The dashboard says so.
- **Track Record** (`/track`):
  - win rate (with 95% range), expectancy, profit factor, max drawdown and losing streak;
  - breakdowns by grade, setup, asset, direction, confidence bucket and timeframe;
  - a 30-day equity curve in R and a calibration chart (predicted vs actual win %);
  - your own trades.

  Groups with negative expectancy over ≥ `MIN_SAMPLE_FOR_STATS` signals get a red warning, also shown on the cards.
- **Scoring rules (conservative):**
  - a limit fills only if price trades through it;
  - if a stop and a target fall inside the same candle, the stop counts first;
  - exits pay taker fees plus slippage;
  - unfilled plans expire as "not triggered" after `PLAN_TTL_MIN` or a 1R runaway;
  - filled plans close at `MAX_HOLD_HOURS`.

## 6. Configuration

Every variable is documented inline in [`.env.example`](.env.example). Weights and risk defaults can also be changed at runtime on the **Settings** page; they are stored in the DB, and "Reset to env" clears them. The Settings page also shows the effective config (secrets masked), per-source health and the scheduled-job heartbeat. The **Usage** page shows Claude API spend (today, this month, all time, per call) and your remaining balance. Anthropic has no balance API, so enter the credit you loaded as `LLM_CREDIT_USD`.

### My trades (watch-only wallet)

Paste your public Hyperliquid address (Phantom: your Ethereum/EVM address) on **My trades**, or set `WATCH_WALLET`. This is the assisted phase: the app only reads, and you make every change.

- **Reads only public data** by address: `clearinghouseState` (positions, liquidation, margin), `frontendOpenOrders` (your TP/SL triggers), `userFillsByTime` and `userFunding`. No key is ever requested, and nothing can trade.
- **When it syncs:** on every scheduled tick (10 min), on page load and every 60 s while the page is open, and on "Sync now". The first sync pulls up to 180 days of fills. With Telegram enabled, it sends a message when a position opens or closes.
- **What it shows for each open position:** entry, mark (live), liquidation, leverage, your stop and target, funding, the app's current call for that coin, and advice on what to change. Advice covers:
  - no stop set;
  - liquidation before or just behind the stop;
  - target closer than the stop (low R:R) and the win rate that needs;
  - stop risk far above the configured % of margin;
  - signal flipped against you;
  - price past TP1, so move the stop to break-even;
  - more leverage than the plan needs.
- **History:** fills are rebuilt into round-trip trades. Each trade shows net P&L after exchange fees, front-end (builder) fees such as Phantom's, and funding. It also shows the exit type (target, stop, manual or liquidated) and R, measured against the first stop seen.
- **Matching to signals:** each trade is matched to the plan that was live when you opened it (the latest plan for that coin within the 12 hours before). The page then compares trades that followed the app with trades that went against it, by coin and by grade. Stored in `wallet_fills`, `wallet_funding` and `wallet_trades`.

### Live prices

Dashboard cards, the asset page header and chart, and the My trades mark price update every 60 seconds in the browser without reloading. Polling pauses while the tab is hidden. Only the current price and 24h change move: entry, stop, targets and liquidation stay as computed until the next analysis. The prices come from `/api/prices` (Hyperliquid contexts cached for about 20 s; DEX prices for 60 s).

## 7. Local development

```bash
uv venv -p 3.12 && uv pip install -e ".[dev]"     # or: python -m venv .venv && pip install -e ".[dev]"
cp .env.example .env                               # SQLite by default
.venv/bin/perpdesk serve --reload                  # http://127.0.0.1:8000
.venv/bin/perpdesk tick                            # one scheduled run (seed, analyse, log, score)
.venv/bin/perpdesk verify --factors                # live proof: prices/funding + full example plans
.venv/bin/pytest --cov=perpdesk                    # tests (property tests, no-lookahead test, fake APIs)
.venv/bin/ruff check src tests api scripts && .venv/bin/mypy src
cd frontend && npm i && npm run build:css          # only if you change templates' Tailwind classes
```

CI (`.github/workflows/ci.yml`) runs ruff, mypy (strict on `analysis/` and `plan.py`), and tests with a coverage gate (>85% on analysis, plan and tracking). **live-verify** runs the real-API proof on every push.

## 8. Architecture

```
src/perpdesk/
  config.py          settings (env + runtime overrides), validated at startup
  sources/           hyperliquid, dexscreener, coingecko/geckoterminal, feargreed, yahoo, calendar, coinglass, binance_hist
  analysis/          indicators, structure, factors, setups, cme_gap, engine (MarketSnapshot -> Analysis)
  plan.py            entry / SL / TPs / size / leverage / liquidation / costs / grade (pure)
  tracking/          scorer (live + backtest), stats, calibration, gates, live scoring, track record
  backtest/          history loader, runner (pass 1 + simulation + walk-forward), report
  pipeline.py        live: incremental fetch -> snapshot -> analyse -> plan -> view; plan logging
  jobs/tick.py       tick (every few min) and daily jobs
  web/               FastAPI app, routes, Jinja templates, static (Tailwind CSS, vendored JS)
  llm/               optional Claude explanation (numeric guard) + usage ledger
  wallet/            watch-only account sync, trade rebuild from fills, position advice
  prices.py          lightweight live prices for the browser poller
  alerts/            optional read-only Telegram
  execution/         Phase 2 interface + NoopExecutor
api/index.py         Vercel entrypoint
```

The same engine runs live and in the backtest. Indicators are causal, and higher-timeframe candles are only used once closed. A test proves that the analysis at bar *T* is identical whether computed on full history, on data truncated at *T*, or on history with the future randomly altered.

## 9. Phase 2 outline

See [`docs/PHASE2.md`](docs/PHASE2.md):

- Hyperliquid API (agent) wallet that can trade but not withdraw;
- modes SIGNALS → PAPER → LIVE (typed confirmation);
- limit entry with exchange-side SL/TP placed atomically (`normalTpsl`) and breakeven after TP1;
- hard caps (open positions, notional, daily loss, drawdown) with auto-halt and a kill switch;
- auto-trading only where live expectancy is positive over ≥ `MIN_SAMPLE_FOR_STATS`;
- idempotent order intents and restart reconciliation.

## 10. Limitations

- Hyperliquid keeps only ~5,000 candles per timeframe, so backtests use Binance's archive for older history (prices differ by a few basis points).
- Open interest and liquidation clusters have no free history: they are shown but not scored, so the engine stays identical to what was backtested.
- Yahoo and ForexFactory are unofficial endpoints and can break. The app then shows "unavailable" and carries on.
- No strategy is guaranteed to work. The app is built to tell you plainly when it doesn't.
