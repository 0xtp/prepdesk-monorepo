# PerpDesk Phase 2 — Automated Execution (design only)

Status: **Stage 1 (paper trading) is implemented; live execution is not.** The `auto/` package runs
the 4h breakout-long strategy in paper mode from the scheduled tick (Auto page, `EXECUTION_MODE=paper`).
Paper trades use live Hyperliquid 1m candles and the backtest's own scorer: same fills, fees, stop-first
rule, TP1 half + break-even, 48h time stop. Safety limits are enforced in paper exactly as they would be
live: daily loss pause, max-drawdown halt, max positions, kill switch. The live executor (agent-wallet
order signing) is not built yet and needs the owner's explicit go-ahead. The app still refuses to start
if `EXECUTION_ENABLED=true`.

### Strategy lab (paper)
Five bots run in paper mode, each with its own balance, limits and record:
- breakout, long and short (positive in every year 2021–2026 in the walk-forward backtest);
- breakdown short;
- trend follower (both sides, trailing stop);
- range trader (both sides);
- funding carry (market-neutral).

The lab bots share their code with `perpdesk lab`, the 2021-onward backtest. Only a bot that holds up in that backtest and in paper should be considered for live.

### Breakout (the first paper bot)
- **Entry:** once per closed 4h bar per coin in `AUTO_COINS`, the normal analysis runs at 4h with breakout
  lookback `AUTO_BREAKOUT_LOOKBACK`. It trades only when the chosen setup is a LONG breakout (the breakout
  fires and agrees with the composite direction), which is the backtest's condition. Market entry; skipped if
  noticed more than `AUTO_MAX_ENTRY_DELAY_MIN` after the close.
- **Size:** `AUTO_RISK_PCT` of paper equity at the stop (fees included); leverage = what the size needs,
  capped at `AUTO_MAX_LEVERAGE` isolated; liquidation must sit `MIN_LIQ_BUFFER_ATR` beyond the stop.
- **Exits:** stop; 50% at TP1 then stop to break-even; rest at TP2; `MAX_HOLD_HOURS` time stop.

### Plan for live (not built)
- Agent wallet on a funded sub-account. The agent can trade but cannot withdraw; the key lives only in
  hosting secrets.
- Entry IOC, then immediately a reduce-only stop (full size) plus TP1/TP2 reduce-only orders. If the
  protective orders fail, flatten at once and halt.
- The tick moves the stop to break-even after TP1 and applies the time stop. Stops rest on the exchange, so
  the 10-minute tick is acceptable at small size.
- Run on testnet first, then a $200–500 mainnet sub-account at 0.5% risk; paper keeps running alongside.

**Before Phase 2: the assisted phase (implemented).** The My trades page watches the user's address read-only, logs every real trade, and advises on stops, targets and leverage. Automation should start only after the record of real, signal-matched trades meets the entry bar:
- at least 10–20 closed trades as a first look;
- at least `MIN_SAMPLE_FOR_STATS` before anything is automated;
- positive net expectancy after all fees, including the front-end builder fee.

`wallet_trades` is that evidence, and Phase 2 reuses its reconciliation (positions and open orders by master address).

## 1. Scope and venue

- **Hyperliquid only.** Phantom has no trading API. Its perps are routed through Hyperliquid anyway, so automating them means automating Hyperliquid directly.
- **Key custody:** orders are signed by a Hyperliquid **API (agent) wallet** approved from the main account with `approveAgent`. Hyperliquid documents that an agent wallet can place and cancel orders but **cannot withdraw or transfer funds**. The main wallet's private key never touches the server.
  - The agent key (`HL_AGENT_PRIVATE_KEY`) lives only in Vercel's encrypted env, or in a separate worker host (see §7).
  - Account queries use the **master** address (`HL_ACCOUNT_ADDRESS`), never the agent address. The agent address returns empty state.
  - Agent addresses are never reused after deregistration, because Hyperliquid can prune their nonce state.
- Isolated margin only, matching Phase 1's liquidation maths.

## 2. Modes

| Mode | What happens | How to enter |
|---|---|---|
| `SIGNALS` | Phase 1 behaviour: plans only. | default |
| `PAPER` | Orders are simulated against the **live L2 book** (`l2Book` snapshots each tick) with the Phase 1 scorer's fill rules. Fills, fees and funding are recorded in a `paper_*` ledger and shown next to real P&L. | Settings → Execution → Paper |
| `LIVE` | Real orders through the agent wallet. | Typed confirmation: the user types `LIVE <account address last 6>`. It expires after 24 h of inactivity and must be re-entered after any config change to caps. |

The mode is stored in the DB with an audit row: who switched it, when, and from which IP.

## 3. Order flow (per plan)

1. **Eligibility** (all must hold):
   - Mode ≠ SIGNALS.
   - Asset and confidence bucket have **positive tracked live expectancy over ≥ `MIN_SAMPLE_FOR_STATS`** signals.
   - The plan's grade is in `AUTO_GRADES` (default `A`).
   - No caution flag in `AUTO_BLOCK_FLAGS` (default: event, stale, liquidity, calibration).
   - All hard caps pass (§4).
2. **Order intent:** an `OrderIntent` row is persisted *before* anything is sent. It has a deterministic `client_order_id` (cloid = first 128 bits of `sha256(plan_id, coin, side, qty)`). Retries reuse it, so a retry can never double-fill.
3. **Leverage:** `updateLeverage(coin, leverage, is_cross=false)`.
4. **One bulk order action** with `grouping: "normalTpsl"`:
   - limit entry at the plan entry, `Alo` (post-only), or `Ioc` for market setups;
   - stop-loss trigger order (`tpsl: "sl"`, `isMarket: true`, reduce-only) for the full size;
   - TP1 trigger order (`tpsl: "tp"`, reduce-only) for 50% of the size;
   - TP2 trigger order (`tpsl: "tp"`, reduce-only) for the remaining 50%.

   Exchange-side SL/TP protect the position even if PerpDesk is offline.
5. **Expiry:** an unfilled entry is cancelled at the plan TTL or when price runs past the entry zone. Because the cloid is deterministic, the cancel is idempotent.
6. **After TP1 fills**, the SL trigger is modified to entry (breakeven) via `modify`. The user-fills websocket or polling detects the fill.
7. **Timeout:** at `MAX_HOLD_HOURS`, any remainder is closed with a reduce-only IOC.

## 4. Hard caps and kill switch

| Cap | Default | Effect when hit |
|---|---|---|
| `MAX_OPEN_POSITIONS` | 3 | new intents rejected |
| `MAX_TOTAL_NOTIONAL_USD` | 3 × default margin × max leverage | new intents rejected |
| `MAX_DAILY_LOSS_USD` | 2 × base risk | **auto-halt**: cancel all entries; positions keep their exchange-side SL/TP |
| `MAX_DRAWDOWN_R` | 10R from equity peak | auto-halt + Telegram alert |
| per-asset cooldown | 1 plan per asset at a time | rejected |

- **Kill switch:** a UI button and a Telegram `/halt` command from allow-listed user IDs. Both set mode to `SIGNALS`, cancel all open entry orders and optionally flatten positions (a second confirmation is required to flatten).
- A halt can only be cleared by the typed confirmation.

## 5. Restart reconciliation

On every tick and at worker start:
1. Load open `OrderIntent`s and DB positions.
2. Query `clearinghouseState` (positions) and `openOrders` / `orderStatus` by cloid for the master account.
3. Reconcile:
   - intent sent but unknown on exchange → mark failed;
   - position on exchange with no intent → alert and attach protective SL/TP if missing;
   - SL/TP missing for an open position → re-place;
   - quantity mismatch → alert and halt.
4. Any unresolved discrepancy halts auto-trading and alerts.

## 6. Integration with an existing SOL bot scaffold

No bot scaffold exists in this repository yet. When it is added (suggested path `bots/sol/`), it should implement the same `Executor` protocol (`place`, `cancel_all`, `move_stop_to_breakeven`, `reconcile`, `halt`). That way PerpDesk can call it in place of the built-in Hyperliquid executor, and the bot can consume PerpDesk plans through the existing JSON view (`/api/assets/{id}/card` payload) or directly from the `plans` table.

## 7. Where it runs

Vercel functions are short-lived, which suits SIGNALS/PAPER. LIVE needs reliable fill handling (websocket for user fills, timely breakeven moves). Recommended: a small always-on worker, for example a $5 VPS or a Fly.io machine. It runs `perpdesk exec-worker` against the same Postgres database and is the only process holding the agent key. The Vercel app only writes intents and reads state.

## 8. Testing before LIVE

- Unit tests for intent idempotency, cap enforcement and reconciliation, using a fake exchange.
- PAPER mode for at least `MIN_SAMPLE_FOR_STATS` trades with live expectancy within the backtest range.
- Hyperliquid **testnet** end-to-end run (`https://api.hyperliquid-testnet.xyz`) with a testnet agent wallet.
- A first LIVE week at minimum size (the `$10` exchange minimum).
