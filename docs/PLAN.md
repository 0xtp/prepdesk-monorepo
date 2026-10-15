# PerpDesk — Phase 1 Plan

Status: **draft, waiting on answers to the open questions (§11)**. No application code has been written yet.

PerpDesk is a self-hosted web app. For each crypto asset it produces a deterministic trade plan for perpetual futures: direction (always LONG or SHORT), grade A/B/C, caution flags, entry, stop, take-profits, size, leverage, liquidation check, and win probability. Every plan is logged and scored against what price did next. Trades are placed by hand on Hyperliquid or in Phantom. Phase 2 (auto-execution) is only designed here (`docs/PHASE2.md`, written later), not built.

---

## 0. Research notes and verification status

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

## 1. Architecture

```
          ┌────────────── worker (APScheduler) ───────────────┐
 sources/ │ refresh ctxs (30s) · candles per TF (TTL by TF)    │
 (httpx)  │ analyse favourites (REFRESH_INTERVAL_SEC)          │──► SQLite (WAL) / Postgres
          │ score open plans (1m) · heartbeat (30s)            │       ▲
          │ nightly: backup, calibration refit, gate refresh   │       │
          └────────────────────────────────────────────────────┘       │
 web (FastAPI + Jinja2 + HTMX) ── reads cached analysis; on-demand refresh ┘
 caddy ── HTTPS reverse proxy
```

- **One analysis engine** (`app/analysis/*`) is a set of pure functions over DataFrames plus a `MarketSnapshot` dataclass. The live worker, the on-demand web refresh, and the backtester all call the same `analyse(snapshot, config) -> Analysis` and `build_plan(analysis, risk_inputs, config) -> Plan`. This is the main protection against differences between live and backtest behaviour.
- **Source layer:** every source returns `SourceResult[T]` = `{ok, data, error, fetched_at, latency_ms, source}`. Failures never raise into the UI. They become "unavailable (error, timestamp)". A per-source health table feeds the Settings page.
- **Cache:** candles live in a DB table `candles(coin, tf, ts, o,h,l,c,v)` that only grows (no rewriting of closed bars). Each refresh fetches only new bars. Responses that are not candles go in an in-memory TTL cache, backed by a `kv_cache` table so restarts stay cheap. The worker also **archives** 1m/5m/1h candles for favourites and open plans. This works around the 5,000-candle cap for the scorer and builds up longer history over time.
- **Web never blocks on slow sources:** a card renders from the latest stored analysis immediately. Refresh uses HTMX swaps with a per-source status line.
- **Process model:** `web` and `worker` are separate containers that share `/data` (DB, cache, reports, backups). A DB advisory row lock makes sure only one worker runs the schedules.

### Directory layout
As in the brief (`perpdesk/app/...`), plus `app/execution/` (Phase 2 interface and no-op), `scripts/verify_sources.py` (the live proof run), and `app/setups/` (named setup rules). Placement inside this monorepo: see Q8.

### Tech
Python 3.12, FastAPI, httpx (async, HTTP/2), pandas, numpy, hand-rolled indicators with reference-value tests (no `pandas-ta`: its maintenance has been unreliable and it breaks on recent numpy/pandas), APScheduler, SQLAlchemy 2 + Alembic, pydantic-settings, Jinja2 + HTMX + Alpine.js, Tailwind standalone CLI (CSS built in the Docker build stage, no Node at runtime), TradingView Lightweight Charts (vendored, pinned), `hypothesis` for property tests, ruff, mypy (strict on `analysis/` and `plan.py`), pytest-cov, GitHub Actions.

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
| `source_health`, `worker_heartbeat` | monitoring |

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

The UI is as specified in the brief: Dashboard cards (sorted by grade then confidence, filter by grade, A/B/C counts, auto-refresh toggle), Search/Add (HTMX instant results across HL perps, then HL spot, then CoinGecko, then DexScreener for addresses), Asset detail (Lightweight Charts with TF switcher, EMA overlays, levels, entry/SL/TP lines, CME gap boxes; factor table; plan panel with margin/risk inputs that recompute through HTMX; copy button; optional LLM explanation; per-asset record), Track Record, Backtests, Settings (masked config, runtime overrides, source health, worker heartbeat). Dark-first, Inter, zinc neutrals, one accent (indigo), green/red only for direction and P&L. Mobile-first grid. Permanent footer: "Signals are probabilistic. Not financial advice. Track record below."

Auth is optional: bcrypt hash in env, signed session cookie, login rate limit (5/min/IP).

---

## 9. Optional extras
- **Telegram** (read-only): alerts on direction flip and upgrade to A; `/check SOL` replies with the current plan; user-ID allowlist.
- **LLM explanation:** Anthropic Messages API. The model gets only the computed facts as JSON, and the system prompt forbids new numbers. **Numeric guard:** every number in the response must appear in the facts payload, or the explanation is discarded and the deterministic text is used instead. Cached per plan hash. Default `LLM_MODEL=claude-sonnet-5-5` when the key is set.

---

## 10. Testing and delivery
- pytest, coverage > 85% on `analysis/`, `plan.py`, `tracking/`. Indicator tests against published reference values (e.g. Wilder RSI/ATR examples) and cross-checks against independent formulas.
- Hypothesis properties: SL on the correct side; SL hit before liq; $ risk ≤ configured risk; qty respects `szDecimals`; leverage ≤ caps.
- Source clients are tested with recorded fixtures (`tests/` only). **No mock data at runtime.**
- `scripts/verify_sources.py`: a live proof that prints BTC/SOL mark, mid, and funding (hourly, 8h, annualised) from HL, plus each source's status, so they can be compared against the HL UI. It runs at the end of every phase. **It cannot run in this build environment until the hosts in Q1 are allowed.**
- CI: ruff, mypy, pytest with coverage, Docker build.
- Deploy: Docker Compose (`web`, `worker`, `caddy`), `/healthz`, nightly SQLite `.backup` with 14-day rotation, log rotation, and a dev compose with hot reload.

---

## 11. Open questions

See the numbered list in the hand-off message. The answers will be recorded here.
