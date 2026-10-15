# PerpDesk — Phase 1 Plan

Status: **all decisions made (§11); Phase 1 built.** See README.md for setup and usage, docs/PHASE2.md for execution design.

PerpDesk is a self-hosted web app. For each crypto asset it produces a deterministic trade plan for perpetual futures: direction (always LONG or SHORT), grade A/B/C, caution flags, entry, stop, take-profits, size, leverage, liquidation check, and win probability. Every plan is logged and scored against what price did next. Trades are placed by hand on Hyperliquid or in Phantom. Phase 2 (auto-execution) is only designed here (`docs/PHASE2.md`, written later), not built.

---

## 0. Research notes and verification status

### Live verification, 2026-10-15 19:14 UTC (GitHub Actions run 38792263319, `scripts/verify_sources.py`)
This sandbox cannot reach the APIs, so the proof run executes in GitHub Actions on every push (`.github/workflows/live-verify.yml`). **All 14 checks passed against the real APIs.**

| coin | mark | funding/h | 8h % | APR % | maxLev | szDecimals | margin table |
|---|---|---|---|---|---|---|---|
| BTC | 82,467.0 | 0.0000125 | 0.0100 | 10.95 | 40 | 5 | 56 |
| ETH | 2,451.3 | 0.0000125 | 0.0100 | 10.95 | 25 | 4 | 55 |
| SOL | 106.01 | 0.0000125 | 0.0100 | 10.95 | 20 | 2 | 54 |
| HYPE | 81.896 | 0.0000125 | 0.0100 | 10.95 | 10 | 2 | 52 |
| PENGU | 0.007615 | −0.0000048 | −0.0038 | −4.21 | 5 | 0 | 5 |
| AERO | 0.90757 | 0.0000125 | 0.0100 | 10.95 | 3 | 0 | 3 |

Facts now **confirmed live** (they replace the matching [verify-live] notes below):
- `funding` is the **hourly** rate. 0.0000125/h is the 0.01%/8h baseline.
- `meta` holds `universe[].marginTableId` and `marginTables` as `[[id, {description, marginTiers:[{lowerBound, maxLeverage}]}]]`. For example, table 52 is "10x below $20M notional, 5x above".
- Candles: 1m → 5,150 bars (3.6 days); **1h → 5,003 bars (208 days)**; 1d → full history (SOL back to 2020-09).
- `fundingHistory` returns 500 rows per page from `startTime` onward, so the client pages forward.
- Every default favourite has a Hyperliquid perp, AERO included (max 3x).
- CoinGecko (no key), DexScreener, Fear & Greed, Yahoo (BTC=F, DXY, S&P 500) and the ForexFactory feed all respond.
- **Binance's live API returns 451 from US datacenters** (GitHub and Vercel both run there). **`data.binance.vision`, Binance's public historical archive, returns 200**, so it can serve as a history source.

The build environment's network policy currently **blocks every data host** (`api.hyperliquid.xyz`, `api.dexscreener.com`, `api.coingecko.com`, `api.alternative.me`, Yahoo Finance) and the docs sites themselves (gitbook, docs.dexscreener.com, docs.coingecko.com). The facts below come from the official Hyperliquid Python SDK source (v0.24.0 from PyPI), official doc excerpts quoted in search results, and secondary sources. Items marked **[verify-live]** are checked by `scripts/verify_sources.py` in the first live run, and the code treats them defensively until then.

### Hyperliquid Info API (`POST https://api.hyperliquid.xyz/info`, JSON body)

| Request | Body | Use |
|---|---|---|
| `meta` | `{"type":"meta"}` | universe: `name`, `szDecimals`, `maxLeverage`, `onlyIsolated`, `isDelisted`, margin-table id **[verify-live]** |
| `metaAndAssetCtxs` | `{"type":"metaAndAssetCtxs"}` | `[meta, ctxs]`; ctx: `markPx`, `oraclePx`, `midPx`, `funding`, `openInterest`, `premium`, `prevDayPx`, `dayNtlVlm`, `impactPxs` |
| `candleSnapshot` | `{"type":"candleSnapshot","req":{"coin","interval","startTime","endTime"}}` (ms) | OHLCV. Intervals: 1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M |
| `l2Book` | `{"type":"l2Book","coin"}` | top-of-book depth, spread, impact for slippage and liquidity |
| `fundingHistory` | `{"type":"fundingHistory","coin","startTime","endTime?"}` | hourly funding history, paginated (≈500 rows per response) |
| `spotMetaAndAssetCtxs` | `{"type":"spotMetaAndAssetCtxs"}` | HL spot tokens (spot-only view for tokens without a perp) |

Constraints that shape the design:
- **Candle history is capped at the most recent 5,000 candles per coin and interval.** That is about 3.5 days of 1m, 17 days of 5m, 52 days of 15m, **208 days of 1h**, 833 days of 4h, and full history on 1d/1w. Time-range responses may also be paged (~500 items), so the client pages by advancing `startTime`. **Consequence:** a 12-month backtest on a 1h trading timeframe is not possible from Hyperliquid candles alone. See Q2.
- **Rate limits:** 1,200 weight per minute per IP, shared across REST. `l2Book` and `allMids` cost 2. Most info requests cost 20. `candleSnapshot` costs 20 plus extra weight per 60 candles returned. The client keeps a token-bucket budget, using ≤ 60% of it by default, and backs off on 429.
- **Funding:** the rate is computed on an 8h basis and paid **hourly at 1/8** of it, capped at 4%/hour. The `funding` field in asset ctxs and `fundingHistory` is the **hourly** rate **[verify-live]**. The app shows the hourly rate, the 8h-equivalent (×8), and the annualised rate (×24×365).
- **Margin / liquidation:** maintenance margin is half the initial margin at max leverage, so maintenance leverage = 2 × max leverage. Some assets have tiered margin tables (larger notional means higher maintenance). For an **isolated** position with size `q`, entry `P0`, isolated margin `M`, and maintenance fraction `l = 1/(2·maxLev)`:
  - Long: `liq = (P0 − M/q) / (1 − l)`
  - Short: `liq = (P0 + M/q) / (1 + l)`

  This matches the docs' form `liq = price − side·margin_available/q/(1 − l·side)` with `margin_available = M − q·P0·l`. When a margin table applies, `l` comes from the tier that matches the notional at the liquidation price. The model is property-tested.
- **Fees (base tier, perps):** taker 4.5 bps, maker 1.5 bps, charged on notional. These match the `.env.example` defaults. Lower tiers and staking discounts are configured through env.
- **Exchange / agent wallets (Phase 2 only):** an API/agent wallet approved via `approveAgent` can place and cancel orders but **cannot withdraw or transfer**. Its nonce state can be pruned when it is deregistered, so agent addresses are never reused. Account queries must use the master address. Orders support `trigger` types (`tpsl: "tp"|"sl"`, `isMarket`) and `grouping: "normalTpsl" | "positionTpsl"`, so the entry, SL, and TP can go in one bulk action.

### Other sources
| Source | Endpoint(s) | Limits / notes |
|---|---|---|
| DexScreener | `GET /latest/dex/search?q=`, `GET /tokens/v1/{chainId}/{addresses}`, `GET /latest/dex/tokens/{address}` (any chain) | no key; ~300 req/min for DEX endpoints; pick the pair with the highest `liquidity.usd` |
| CoinGecko | `GET /api/v3/search`, `/coins/{id}`, `/coins/markets`; header `x-cg-demo-api-key` | Demo: ~30 calls/min, 10k/month, so cache aggressively (search results 24h, metadata 7d) |
| Fear & Greed | `GET https://api.alternative.me/fng/?limit=N&format=json` | updates daily; cached 1h; attribution shown in UI |
| CME BTC futures | `yfinance` ticker `BTC=F`, daily | unofficial, best-effort; cached 12h; shows "unavailable" on failure |
| Macro (opt.) | `yfinance` `DX-Y.NYB` (DXY), `^GSPC` (S&P 500) | same caveats; off when `ENABLE_MACRO=false` |
| Event calendar (opt.) | `https://nfs.faireconomy.media/ff_calendar_thisweek.json` (ForexFactory weekly feed) | unofficial, undocumented; filter `impact == "High"` and `country == "USD"`; times in US/Eastern, converted to UTC |
| Coinglass (opt.) | v4 open API (`CG-API-KEY` header): liquidation heatmap/levels, aggregated OI, long/short ratio | off without key; endpoint paths confirmed at build time against their docs |

---

## 1. Architecture (Vercel)

Deployment target is **Vercel**. That rules out a long-running worker, a local SQLite file (the filesystem is ephemeral), Docker and Caddy (Vercel provides HTTPS). The design becomes:

```
 Vercel project
 ├── FastAPI app (Python serverless function, Jinja2 + HTMX pages, JSON/HTMX endpoints)
 ├── /api/cron/* endpoints (secured with CRON_SECRET) ── fired by the scheduler (below)
 │      tick:   refresh ctxs + incremental candles for favourites, analyse, log plans, score open plans
 │      daily:  calibration refit, live-gate check, cleanup, Anthropic cost sync
 └── static: Tailwind CSS (built at deploy), HTMX, Alpine, Lightweight Charts (vendored)

 Neon Postgres (Vercel Marketplace, free tier) ── all state: candles, plans, outcomes, stats, settings, health, LLM usage
 GitHub Actions ── backtests + walk-forward gates (heavy CPU, minutes long) → writes setup_stats + HTML reports into Postgres
                ── live-verify workflow (real API proof run on every push)
```

- **Scheduler:** Vercel Cron on the **Hobby plan only fires once per day**; Pro fires down to once a minute. To log and score plans you need a tick every 5–15 minutes. Options: (a) Vercel Pro ($20/mo), cron every 5 min; (b) Hobby + GitHub Actions `schedule` (`*/10`, free on public repos; schedules can lag 5–15 min); (c) Hobby + a free external pinger (cron-job.org) hitting `/api/cron/tick` with the secret. The app supports all three: the tick endpoint is idempotent and safe to call at any frequency. See Q10.
- **Scorer resilience:** the scorer refetches 1m candles from Hyperliquid for each open plan from its creation time, and Hyperliquid keeps ~3.5 days of 1m bars. So ticks can be irregular without losing accuracy, as long as there is at least one tick every ~3 days. Beyond that it falls back to 5m (17 days) and 15m, and records which resolution it used.
- **Function limits:** Python bundles up to 500 MB (pandas + numpy fit; no scipy or scikit-learn, so isotonic regression and Wilson intervals are hand-written). Each tick processes favourites in batches within the function time limit and resumes where it stopped on the next tick (cursor stored in the DB).
- **Rate limits:** candles are stored once and fetched incrementally (only new bars), so a tick across ~15 favourites × 5 timeframes costs a few hundred weight against Hyperliquid's 1,200/min. Vercel functions share egress IPs with other tenants, so a 429 backs off and the card shows the last good data with its timestamp.
- **One engine:** live ticks, on-demand refresh, and the GitHub Actions backtester all import the same `perpdesk.analysis` and `perpdesk.plan` package.
- **Local dev:** `uvicorn` + SQLite (or a local Postgres via `docker compose -f docker-compose.dev.yml up`). The same code paths are used, and a CLI `perpdesk tick` replaces the cron.
- **Self-hosting fallback:** a Dockerfile is kept so the app can also run on a VPS, but Vercel is the documented path.

### Repository layout (single repo)

```
prepdesk-monorepo/
  src/perpdesk/                 # one installable Python package (the engine + web app)
    config.py                   # pydantic-settings, validated at import, fails fast
    db/                         # SQLAlchemy models, session, Alembic migrations
    sources/                    # hyperliquid, dexscreener, coingecko, feargreed, cme, macro, calendar, coinglass, binance_hist
    assets/                     # registry (symbol/name/contract resolution), favourites
    analysis/                   # indicators, structure, derivatives, regime, cme_gap, scoring, setups
    plan.py                     # entry/SL/TP/size/leverage/liq/costs (pure, mypy strict)
    tracking/                   # scorer, stats, calibration, gates
    backtest/                   # runner, walk-forward, reports
    llm/                        # explain.py (Claude API), usage.py (token + cost ledger)
    alerts/                     # telegram (webhook mode)
    execution/                  # Phase 2 interface + NoopExecutor
    jobs/                       # tick, daily (called by cron endpoints and CLI)
    web/                        # FastAPI app, routes, templates, static
    cli.py                      # perpdesk tick | backtest | verify | migrate
  api/index.py                  # Vercel entrypoint: `from perpdesk.web.main import app`
  tests/                        # unit, property, no-lookahead, recorded fixtures (only place mocks exist)
  scripts/verify_sources.py     # live proof run
  docs/                         # PLAN.md, PHASE2.md
  .github/workflows/            # ci.yml, live-verify.yml, backtest.yml, tick.yml (if option b)
  vercel.json, pyproject.toml, .env.example, README.md, Dockerfile, docker-compose.dev.yml
```
One package rather than several: the web app, cron jobs and backtester must share the exact same engine code, and a single Python package is the simplest way to guarantee that. Phase 2 execution lives in the same package behind a flag.

---

## 2. Data model (main tables)

| Table | Purpose |
|---|---|
| `assets` | resolved assets: symbol, name, HL perp coin (nullable), HL spot, CoinGecko id, chain + contract, class, `perp_available`, `sz_decimals`, `max_leverage`, `only_isolated` |
| `favorites`, `favorite_groups` | order (`position`), group, persisted drag-and-drop |
| `candles`, `funding`, `asset_ctx_snapshots` | market data store (OI history comes from our own ctx snapshots every 5 min, because HL has no OI-history endpoint) |
| `analyses` | per asset per run: factor scores per TF (JSON), regime, flags, score, confidence |
| `plans` | every plan: inputs hash, config snapshot, setup, direction, grade, flags, entry zone, SL, TPs, size, leverage, liq, costs, win-prob, EV, TTL |
| `plan_outcomes` | scorer result: filled?, fill ts/price, exit reason (SL/TP1/TP2/BE/timeout/not-triggered), R, MFE, MAE |
| `user_trades` | "I took this": actual fill, size, optional exit, tracked separately |
| `setup_stats` | backtest OOS stats per setup × scope (asset / class / all) × direction, gate results, quarterly breakdown, and the count of parameter combinations tried |
| `calibration_models` | fitted isotonic/Platt params per setup scope, with fit date and sample size |
| `settings_overrides` | runtime-edited weights and risk defaults (with "reset to env") |
| `source_health`, `job_heartbeat` | monitoring (last tick, last daily job, last backtest) |
| `llm_usage` | every Claude call: model, input/output/cache tokens, computed $ cost, plan id |

---

## 3. Analysis engine (deterministic)

Timeframes: `TIMEFRAMES` (default 1w,1d,4h,1h,15m). Each factor returns `Factor(name, group, tf, score ∈ [−2, +2], reason, source, as_of)`. Indicators are computed only on **closed** bars. The forming bar is used only for "current price".

| Group | Factors |
|---|---|
| Trend | price vs EMA20/50/200 per TF; EMA stack alignment + slope; 50W/200W MA distance; HH/HL vs LH/LL structure on 1d/4h |
| Momentum | RSI(14) level + regular divergence (pivot-based); MACD(12,26,9) cross + histogram slope |
| Volatility | ATR(14) + 1-year ATR percentile; Bollinger(20,2) bandwidth squeeze; ADX(14) range vs trend. Feeds flags and the plan more than direction |
| Structure | fractal swing highs/lows (N=3/5), clustered S/R (ATR-tolerance clustering), PDH/PDL, PWH/PWL, equal highs/lows (liquidity pools), session VWAP (UTC day), volume profile POC/VAH/VAL (lookback 1d/1w) |
| Derivatives | hourly funding (current, 24h/7d mean, z-score), OI Δ 1h/4h/24h vs price Δ (four-quadrant read), premium; crowding flag; Coinglass liquidation clusters (if configured) |
| Regime | BTC 1d/4h trend; alt 30d return correlation + relative strength vs BTC; volatility regime; macro risk-on/off (DXY, SPX) |
| Sentiment | Fear & Greed contrarian at extremes (low weight); CME gap info (low weight, info only) |

**Scoring:** `S = Σ_group W_group · Σ_tf w_tf · mean(factor scores)` normalised to [−1, +1]. TF weights put more weight on higher timeframes (default 1w 0.30, 1d 0.25, 4h 0.20, 1h 0.15, 15m 0.10; configurable). Then:
- direction = sign(S). If |S| < ε, the direction comes from the HTF (1d) trend and the grade is forced to C.
- confidence = 0–100 from |S| plus agreement across groups (a conflict penalty).
- expected move (4–24h) = UP / DOWN / SIDEWAYS. SIDEWAYS applies when ADX < 20, the BB bandwidth is low, and price is inside a detected range. In that case the plan switches to a range trade.
- top 3 factors for and against, ranked by weighted contribution.

**BTC filter:** an alt LONG against a strong BTC downtrend (1d and 4h both bearish) gets a caution flag, unless relative strength vs BTC is clearly positive. The mirror rule applies to shorts.

---

## 4. Setup library and grading

Named, rule-based setups with few, round-number parameters:

1. **Trend pullback.** HTF (1d+4h) trend agrees; price pulls back to the 1h EMA20–50 zone; trigger is a bullish/bearish close back in the trend direction.
2. **Range-edge reclaim / reject.** A detected range (ADX < 20, ≥ 2 touches per edge); price sweeps the edge and closes back inside.
3. **Breakout with OI expansion.** A close beyond a 20-bar range high/low with OI up ≥ X% over 4h and volume > 1.5× average.
4. **Funding-crowded fade.** Funding z-score > 2 (or < −2) with price extended > 2 ATR from EMA20 and momentum divergence; fade toward the mean.
5. **Composite bias (fallback).** No named setup fires, so the plan follows the composite score. **Max grade B. It can never be A.**

Grade logic:
- **A** only if the plan's setup × asset (or class/all fallback) × direction passes **every** statistical gate (§6) **and** is not live-demoted.
- Otherwise **B** if confidence ≥ `MIN_CONFIDENCE`, else **C**.
- R:R below `MIN_RR` after adjustment means drop one grade and add a flag. Low liquidity caps the grade at C.
- **Caution flags:** event blackout, ATR percentile > `MAX_ATR_PCTL`, stale data (any required source older than 2× its TTL), low liquidity, liquidation buffer failed (leverage lowered), BTC counter-trend, crowded funding, R:R below minimum, calibration drift.

---

## 5. Trade plan maths (`plan.py`, pure and typed)

Inputs: analysis, `margin_usd`, risk (`RISK_USD` or `RISK_PCT_OF_MARGIN`), asset meta (`szDecimals`, `maxLeverage`, margin tiers), and fees.

1. **Entry:** market = current mark/mid. The limit zone is the nearest sensible pullback level in the trade direction, within 1 ATR (VWAP, a reclaimed level, EMA20, or the FVG midpoint). The plan states when to use each ("limit if price is above the zone; market if a breakout setup has already triggered").
2. **Stop:** beyond the nearest invalidating structure level ± `SL_ATR_BUFFER`·ATR(trading TF). Enforce `|entry − SL| ≥ MIN_SL_ATR`·ATR.
3. **TPs:** TP1 = the more conservative of (first opposing level, entry ± 1.5R). TP2 = next level, clamped to [2.5R, 3R]. Split 50/50, then move the stop to breakeven after TP1. A range trade uses TP1 = range mid and TP2 = the opposite edge.
4. **Costs:** entry fee (maker for a limit, taker for a market) + exit fee (taker, conservative) + slippage (from `l2Book` impact for the computed size, min 1 bp) + expected funding = hourly rate × expected hold hours × side sign. Funding can be a credit, but it is only counted when it is a cost (conservative). R:R uses net reward / net risk.
5. **Risk $:** `base_risk × GRADE_x_SIZE × (CAUTION_SIZE if flagged)`.
6. **Size:** `notional = risk_usd / stop_pct`; `lev = ceil(notional / margin)` capped at `min(MAX_LEVERAGE, asset maxLeverage)`. If capped, `notional = margin × lev_cap` and the reduced risk is reported. `qty = floor_to(szDecimals, notional / entry)`. Rounding down means $ risk ≤ target (property-tested).
7. **Liquidation check (isolated):** compute `liq` (§0). Require `|liq − SL| ≥ MIN_LIQ_BUFFER_ATR · ATR` beyond the stop. If it fails, step leverage down: the margin used rises up to `margin_usd`; if leverage is already at the minimum, notional shrinks. Repeat until it passes, then add the "liq buffer" flag.
8. **Outputs:** notional, qty, leverage, $ risk, $ at TP1/TP2 (net), fees, liq price and its distance from the SL, win probability with a 95% interval and n, break-even win rate `= 1/(1 + RR)`, EV $ = `p·win$ − (1−p)·risk$`, TTL countdown, invalidation text (generated from the SL structure level and TF), and a copy-to-clipboard block in the format from the brief.

Spot-only assets (no HL perp): the same analysis on DEX/spot candles. Leverage is never shown, and the plan is labelled "Spot view only, no perp market".

---

## 6. Tracking, statistics, and gates

**Scorer** (every minute): for each unresolved plan, replay archived 1m candles, falling back to 5m and then 15m if the archive has gaps (and recording which resolution was used):
- not filled before TTL / price leaving the zone → `not_triggered`;
- when SL and TP fall inside the same candle, assume **SL first** (conservative);
- TP1 partial, then BE stop, then TP2 / BE / timeout (`MAX_HOLD_HOURS`, new env var, default 48h on a 1h TF). Exit at the close, with costs;
- record R (net of costs), MFE, and MAE in R.

**Stats** (per asset, direction, setup, grade, confidence bucket, TF): n, win rate + Wilson 95% interval, avg R, expectancy, profit factor, max DD (R), max losing streak, rolling 30-day equity curve in R. A red warning appears when expectancy < 0 over ≥ `MIN_SAMPLE_FOR_STATS`.

**Backtest gate** (per setup × scope × direction), all required for A:
- ≥ `GATE_MIN_TRADES` OOS trades;
- **walk-forward:** rolling train/test (e.g. 6-month train / 3-month test, stepped by 3 months). Parameters are chosen only from a tiny round-number grid on train and scored on test. The grid size is recorded in the report;
- OOS expectancy > `GATE_MIN_EXPECTANCY_R` net of fees, slippage, and funding;
- Wilson lower bound of the win rate > break-even win rate at the setup's average R:R;
- PF > `GATE_MIN_PF`, max DD < `GATE_MAX_DD_R`;
- positive in ≥ 3 of the last 4 OOS quarters;
- a **held-out final period** (last 3 months of available history) is evaluated once per release and stored separately. It is not used for selection.

**Live gate:** with ≥ `MIN_SAMPLE_FOR_STATS` live plans, an A setup is demoted to B if live expectancy < 0 or the rolling 30-trade R falls below the 5th percentile of the bootstrap distribution of 30-trade windows from OOS trades. It is promoted back only after re-passing.

**Win probability:** P(TP1 before SL) from OOS outcomes at the most specific scope with data (setup×asset×dir → setup×class×dir → setup×all×dir; the scope used is shown), recalibrated on confidence with **isotonic regression** (Platt if n < 200). The 95% interval is Wilson (or bootstrap). n < `MIN_SAMPLE_FOR_STATS` shows "~ low sample".

**Calibration:** a reliability chart per 10-pt bucket. If the weighted mean |predicted − actual| > `MAX_CALIBRATION_ERROR` (with ≥ `MIN_SAMPLE_FOR_STATS` resolved), a warning shows on every card.

**TAKE / SKIP:** TAKE if grade A, or if that grade's live expectancy is > 0 over ≥ `MIN_SAMPLE_FOR_STATS`. Otherwise SKIP. SKIP plans are still shown, not hidden.

**Honest expectation:** at launch, live samples are zero. A-grades exist only if a setup passes the backtest gates, and it is quite likely that **no setup passes initially**. In that case every call is B/C and the dashboard says so plainly.

---

## 7. Backtester

- Event loop over closed bars of the trading TF. At each bar it builds a `MarketSnapshot` truncated to `ts ≤ bar_close` for every TF (higher TFs include only bars **closed** by then), the funding known by then, and OI history up to then. It calls the same `analyse` and `build_plan`, then simulates fills on later bars with the scorer's rules.
- **No-lookahead test:** run the engine at bar *t* on (a) data truncated at *t* and (b) the full dataset with future bars added. Plans must be identical. Future bars are also randomly perturbed, and plans up to *t* must not change.
- Costs: taker/maker fees, slippage (conservative fixed bps per asset class, e.g. 3 bps majors / 8 bps alts), and hourly funding from `fundingHistory`.
- Factor ablation: rerun with each factor group zeroed and report the Δ expectancy.
- OI-dependent factors and setups: HL has **no historical OI endpoint**, so OI factors are excluded from backtests until our own snapshot archive has enough history (Coinglass history if a key is configured). The report states this.
- Output: an HTML report per asset/setup (Jinja + inline SVG charts) in `/data/reports`, viewable in the UI, with a "sample too small to conclude" banner when n < `GATE_MIN_TRADES`, and a list of limitations.

---

## 8. UI

The UI is as specified in the brief: Dashboard cards (sorted by grade then confidence, filter by grade, A/B/C counts, auto-refresh toggle), Search/Add (HTMX instant results across HL perps, then HL spot, then CoinGecko, then DexScreener for addresses), Asset detail (Lightweight Charts with TF switcher, EMA overlays, levels, entry/SL/TP lines, CME gap boxes; factor table; plan panel with margin/risk inputs that recompute through HTMX; copy button; optional LLM explanation; per-asset record), Track Record, Backtests, Settings (masked config, runtime overrides, source health, job heartbeat) and a **Usage** page (Claude API spend and balance, data-source call counts). Dark-first, Inter, zinc neutrals, one accent (indigo), green/red only for direction and P&L. Mobile-first grid. Permanent footer: "Signals are probabilistic. Not financial advice. Track record below."

Auth is optional: bcrypt hash in env, signed session cookie, login rate limit (5/min/IP).

---

## 9. Optional extras
- **Telegram** (read-only): alerts on direction flip and upgrade to A; `/check SOL` replies with the current plan; user-ID allowlist.
- **LLM explanation:** Anthropic Messages API. The model gets only the computed facts as JSON, and the system prompt forbids new numbers. **Numeric guard:** every number in the response must appear in the facts payload, or the explanation is discarded and the deterministic text is used instead. Cached per plan hash. Default `LLM_MODEL=claude-sonnet-5-5` ($2 / $10 per million input/output tokens; roughly $0.005–0.01 per explanation).
- **Usage and balance page:** every call's `usage` (input, output and cache tokens) is stored in `llm_usage` and priced from a model price table, giving today, month-to-date and all-time spend plus a per-model breakdown. **Anthropic has no API that reports remaining prepaid credit**, so balance = `LLM_CREDIT_USD` (what you loaded, entered in Settings) minus spend tracked since `LLM_CREDIT_SINCE`. Optional `ANTHROPIC_ADMIN_KEY` (an `sk-ant-admin…` key): the daily job pulls `GET /v1/organizations/cost_report`, which shows org-wide actual billed cost next to the app's own figure. Optional `LLM_MONTHLY_BUDGET_USD`: explanations switch off automatically when it is reached.

---

## 10. Testing and delivery
- pytest, coverage > 85% on `analysis/`, `plan.py`, `tracking/`. Indicator tests against published reference values (e.g. Wilder RSI/ATR examples) and cross-checks against independent formulas.
- Hypothesis properties: SL on the correct side; SL hit before liq; $ risk ≤ configured risk; qty respects `szDecimals`; leverage ≤ caps.
- Source clients are tested with recorded fixtures (`tests/` only). **No mock data at runtime.**
- `scripts/verify_sources.py`: a live proof that prints BTC/SOL mark, mid, and funding (hourly, 8h, annualised) from HL, plus each source's status, so they can be compared against the HL UI. It runs at the end of every phase. **It cannot run in this build environment until the hosts in Q1 are allowed.**
- CI: ruff, mypy, pytest with coverage. `live-verify` runs the real-API proof on every push.
- Deploy: Vercel (GitHub integration, preview deploy per branch), Neon Postgres (Neon's point-in-time restore replaces nightly backups), `/healthz`, a cron heartbeat shown in the UI, Vercel logs. Local dev uses uvicorn with hot reload.

---

## 11. Decisions (round 1 answers)

1. **Live data access:** proof runs in GitHub Actions (passing, see §0). The deployed app on Vercel calls the APIs directly.
2. **Backtest history:** Binance archive candles (`data.binance.vision`) for history before Hyperliquid's 5,000-bar window, Hyperliquid candles inside it, Hyperliquid funding throughout (decided).
3. **Margin mode:** always isolated.
4. **Risk:** `RISK_PCT_OF_MARGIN=8` default, validated ≤ 10. Optional `ACCOUNT_EQUITY_USD` shows the risk as a % of the whole account, with a warning above 2%.
5. **Default favourites (groups):** Majors: BTC, ETH · Large: SOL, HYPE, AVAX, LINK, SUI · Mid: UNI, ONDO, ZEC, AERO, ENA · Memes: PENGU, DOGE. All resolve to Hyperliquid perps (checked live). Gate fallback classes use the same groups.
6. **Conservative scoring:** `MAX_HOLD_HOURS=48`, taker fees on exits, and SL counted first when SL and TP fall in the same candle. Accepted.
7. **Context sources:** event calendar on (used as a risk flag). CME gap on, info only, weight ≈ 0. Macro (DXY/S&P 500) **on but weight 0 until backtest ablation shows it helps**. Yahoo is called directly through its chart JSON endpoint, not the heavy `yfinance` package.
8. **Layout:** a single repo with one Python package, `src/perpdesk` (see §1).
9. **LLM:** `claude-sonnet-5-5`, plus a usage and balance page.
10. **Scheduler:** needed so plans are logged and scored on a fixed schedule (honest track record, gates, alerts) rather than only when the app is opened. Default: Vercel Hobby + the included GitHub Actions `tick` workflow (free). Vercel cron runs the daily job.
11. **Database:** the user's Supabase Postgres (transaction pooler, port 6543); Neon also works. SQLite for local/tests.
12. **Confirmed live:** Hyperliquid weekly candles are epoch-aligned (weeks start Thursday 00:00 UTC); the backtester resamples the same way.
