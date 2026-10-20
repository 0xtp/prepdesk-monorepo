"""Application settings. Loaded from environment (and `.env` locally), validated at startup.

Runtime-editable keys (weights, risk defaults) can be overridden from the Settings page; those
overrides live in the DB and are merged on top of env values via `effective_settings()`.
"""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

VALID_TFS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M")

# Keys that may be edited at runtime from the Settings page.
RUNTIME_KEYS = (
    "weights_trend",
    "weights_momentum",
    "weights_structure",
    "weights_derivatives",
    "weights_regime",
    "weights_sentiment",
    "weights_macro",
    "min_confidence",
    "default_margin_usd",
    "risk_pct_of_margin",
    "risk_usd",
    "max_leverage",
    "min_rr",
    "grade_a_size",
    "grade_b_size",
    "grade_c_size",
    "caution_size",
    "sl_atr_buffer",
    "min_sl_atr",
    "min_liq_buffer_atr",
    "plan_ttl_min",
    "account_equity_usd",
    "watch_wallet",
    "execution_mode",
    "auto_coins",
    "auto_risk_pct",
    "auto_max_leverage",
    "auto_max_positions",
    "auto_daily_loss_pct",
    "auto_max_drawdown_pct",
    "auto_paper_equity",
    "auto_breakout_lookback",
    "auto_max_entry_delay_min",
    "auto_max_entry_drift_r",
    "room_check_filter",
    "breakout_short_take",
    "alert_account_usd",
    "alert_risk_pct",
    "alert_tz",
    "alert_quiet_hours",
    "alert_daily_hour",
    "alerts_take",
    "alerts_closes",
    "alerts_health",
    "alerts_wallet",
    "alerts_general_flips",
    "take_min_live_trades",
    "lab_bots",
    "lab_coins",
    "trend_extra_coins",
    "trend_extra_half_until",
    "moonshot_wallet",
    "moonshot_budget_usd",
    "llm_credit_usd",
    "llm_monthly_budget_usd",
)

SECRET_KEYS = (
    "database_url",
    "ui_password_hash",
    "session_secret",
    "coingecko_api_key",
    "coinglass_api_key",
    "anthropic_api_key",
    "anthropic_admin_key",
    "telegram_bot_token",
    "telegram_webhook_secret",
    "cron_secret",
    "hl_agent_private_key",
)


def _csv(v: Any) -> Any:
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    # --- App ---
    app_name: str = "PerpDesk"
    database_url: str = "sqlite:///./perpdesk.db"
    tz: str = "Asia/Dubai"
    log_level: str = "INFO"
    auth_enabled: bool = False
    ui_password_hash: str = ""
    session_secret: str = ""
    public_domain: str = ""
    cron_secret: str = ""

    # --- Data sources ---
    hyperliquid_api_url: str = "https://api.hyperliquid.xyz"
    coingecko_api_key: str = ""
    coinglass_api_key: str = ""
    enable_cme_gap: bool = True
    enable_macro: bool = True
    enable_event_calendar: bool = True
    default_favorites: str = "Majors:BTC,ETH|Large:SOL,HYPE,AVAX,LINK,SUI|Mid:UNI,ONDO,ZEC,AERO,ENA|Memes:PENGU,DOGE"
    refresh_interval_sec: int = Field(120, ge=15)
    hl_rate_budget_per_min: int = Field(700, ge=50, le=1200)

    # --- Analysis ---
    timeframes: Annotated[list[str], NoDecode] = ["1w", "1d", "4h", "1h", "15m"]
    trading_timeframe: str = "1h"
    tf_weights: Annotated[list[float], NoDecode] = [0.30, 0.25, 0.20, 0.15, 0.10]
    min_confidence: int = Field(60, ge=0, le=100)
    max_atr_pctl: float = Field(95, ge=50, le=100)
    event_blackout_hours: float = Field(2, ge=0)
    min_liquidity_usd: float = Field(2_000_000, ge=0)
    weights_trend: float = Field(0.30, ge=0)
    weights_momentum: float = Field(0.15, ge=0)
    weights_structure: float = Field(0.20, ge=0)
    weights_derivatives: float = Field(0.15, ge=0)
    weights_regime: float = Field(0.15, ge=0)
    weights_sentiment: float = Field(0.05, ge=0)
    weights_macro: float = Field(0.0, ge=0)

    # --- Trade plan & risk ---
    default_margin_usd: float = Field(250, gt=0)
    risk_pct_of_margin: float = Field(8, gt=0, le=10)
    risk_usd: float | None = Field(None, gt=0)
    account_equity_usd: float | None = Field(None, gt=0)
    max_leverage: int = Field(10, ge=1, le=50)
    min_rr: float = Field(1.5, gt=0)
    grade_a_size: float = Field(1.0, ge=0, le=1)
    grade_b_size: float = Field(0.5, ge=0, le=1)
    grade_c_size: float = Field(0.25, ge=0, le=1)
    caution_size: float = Field(0.5, ge=0, le=1)
    sl_atr_buffer: float = Field(0.3, ge=0)
    min_sl_atr: float = Field(0.8, gt=0)
    min_liq_buffer_atr: float = Field(3, ge=0)
    plan_ttl_min: int = Field(60, ge=5)
    max_hold_hours: int = Field(48, ge=1)
    taker_fee_bps: float = Field(4.5, ge=0)
    maker_fee_bps: float = Field(1.5, ge=0)
    slippage_bps_major: float = Field(3, ge=0)
    slippage_bps_alt: float = Field(8, ge=0)

    # --- Tracking & quality gates ---
    min_sample_for_stats: int = Field(30, ge=5)
    gate_min_trades: int = Field(100, ge=10)
    gate_min_expectancy_r: float = 0.15
    gate_min_pf: float = 1.3
    gate_max_dd_r: float = 15
    max_calibration_error: float = Field(0.08, gt=0, lt=1)

    # --- LLM ---
    anthropic_api_key: str = ""
    anthropic_admin_key: str = ""
    llm_model: str = "claude-sonnet-5-5"
    llm_credit_usd: float | None = Field(None, ge=0)
    llm_credit_since: date | None = None
    llm_monthly_budget_usd: float | None = Field(None, ge=0)

    # --- Watch-only wallet (My trades page). Public address only; never a private key. ---
    watch_wallet: str = ""

    # --- Telegram ---
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = []
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""  # empty = derived from the bot token
    # Alerts (alerts/notify.py). Account size and risk only shape the suggested size in each TAKE alert.
    alert_account_usd: float | None = Field(None, gt=0)
    alert_risk_pct: float = Field(1.0, gt=0, le=5)
    alert_tz: str = "UTC"  # IANA name (Asia/Kolkata) or a UTC offset (+05:30)
    alert_quiet_hours: str = ""  # local "HH-HH", e.g. 01-07; nothing but kill-switch/halt alerts is sent then
    alert_daily_hour: int | None = Field(8, ge=0, le=23)  # local hour for the daily summary; empty = off
    alerts_take: bool = True  # TAKE signals from the proven paper bots
    alerts_closes: bool = True  # a TAKE trade hit its target or stop; bot paused / halted
    alerts_health: bool = True  # scheduler stopped running
    alerts_wallet: bool = True  # your own Hyperliquid positions opened / closed
    alerts_general_flips: bool = False  # old alert: general-analysis direction flips (those signals lost money)
    # Breakout SHORT lost money in 2024-2026 in the backtest: the bot keeps paper-trading it, cards show SKIP.
    breakout_short_take: bool = False

    # --- Phase 2: auto-trading (paper now; live is not built yet) ---
    # signals = no auto trades; paper = simulated trades against live prices, no keys, no orders.
    execution_mode: Literal["signals", "paper"] = "signals"
    # Breakout bot coins. The 2021-2026 4h walk-forward was positive pooled across all 12, long and short; no
    # per-coin cherry-picking (that would fit the noise).
    auto_coins: Annotated[list[str], NoDecode] = ["SOL", "BTC", "ETH", "HYPE", "AVAX", "LINK", "SUI", "UNI", "DOGE",
                                                  "ENA", "ONDO", "ZEC"]
    auto_risk_pct: float = Field(0.5, gt=0, le=2)  # % of trading equity lost if a stop is hit
    auto_max_leverage: int = Field(3, ge=1, le=5)
    auto_max_positions: int = Field(3, ge=1, le=10)
    auto_daily_loss_pct: float = Field(3.0, gt=0, le=20)  # halt new entries for the UTC day
    auto_max_drawdown_pct: float = Field(10.0, gt=0, le=50)  # halt until manually resumed
    auto_paper_equity: float = Field(500.0, gt=0)
    auto_breakout_lookback: int = Field(20, ge=5, le=200)
    # A signal is still taken if noticed late (schedulers are irregular), up to one full 4h bar, but only while the
    # price has not moved more than AUTO_MAX_ENTRY_DRIFT_R × the stop distance in the trade's direction.
    auto_max_entry_delay_min: int = Field(240, ge=5, le=240)
    auto_max_entry_drift_r: float = Field(0.3, ge=0, le=2)
    # Skip a proven-bot signal when a strong support/resistance sits within 0.5R in the trade's way. Off by default:
    # the 2021-2026 backtest did not show it improving results (see docs/PHASE2.md, "Room check").
    room_check_filter: bool = False
    # The general (composite) plan only shows TAKE after this many live trades with a positive, significant edge.
    take_min_live_trades: int = Field(100, ge=30)
    # Strategy-lab bots paper-traded alongside the breakout bot (each with its own AUTO_PAPER_EQUITY balance).
    # Defaults are the bots that were positive in the 2021+ backtest; breakdown_short and range_revert lost money.
    lab_bots: Annotated[list[str], NoDecode] = ["trend_follow", "funding_carry"]
    lab_coins: Annotated[list[str], NoDecode] = ["SOL", "BTC", "ETH", "HYPE", "AVAX", "LINK", "SUI", "UNI", "DOGE",
                                                 "ENA", "ONDO", "ZEC"]
    # Extra coins for the trend follower only: picked 2026-10-20 as the 7 of 24 tested coins that were positive overall
    # and in most years (2021-2026). Picked after seeing results, so expect less; alerts say "half size" until the date.
    trend_extra_coins: Annotated[list[str], NoDecode] = ["BNB", "ADA", "TRX", "FIL", "OP", "ARB", "WLD"]
    trend_extra_half_until: str = "2026-12-20"
    # Moonshot bucket: a separate Hyperliquid (sub-)account with money you can afford to lose. Watch-only.
    moonshot_wallet: str = ""
    moonshot_budget_usd: float | None = Field(None, gt=0)
    execution_enabled: bool = False
    hl_account_address: str = ""
    hl_agent_private_key: str = ""

    @field_validator("risk_usd", "account_equity_usd", "llm_credit_usd", "llm_monthly_budget_usd", "llm_credit_since",
                     mode="before")
    @classmethod
    def _empty_none(cls, v: Any) -> Any:
        return None if v == "" else v

    @field_validator("timeframes", mode="before")
    @classmethod
    def _tfs(cls, v: Any) -> Any:
        v = _csv(v)
        for tf in v:
            if tf not in VALID_TFS:
                raise ValueError(f"unsupported timeframe {tf!r}; valid: {VALID_TFS}")
        return v

    @field_validator("tf_weights", mode="before")
    @classmethod
    def _tfw(cls, v: Any) -> Any:
        v = _csv(v)
        return [float(x) for x in v]

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> Any:
        return [int(x) for x in _csv(v)] if v not in (None, "") else []

    @field_validator("alert_tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        from perpdesk.alerts.clock import parse_tz

        v = (v or "UTC").strip()
        if parse_tz(v) is None:
            raise ValueError("ALERT_TZ must be an IANA name like Asia/Kolkata or an offset like +05:30")
        return v

    @field_validator("alert_quiet_hours")
    @classmethod
    def _quiet(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not re.fullmatch(r"([01]?\d|2[0-3])-([01]?\d|2[0-3])", v):
            raise ValueError("ALERT_QUIET_HOURS must look like 01-07 (local hours, from-to), or be empty")
        return v

    @field_validator("alert_account_usd", "alert_daily_hour", "moonshot_budget_usd", mode="before")
    @classmethod
    def _blank_none(cls, v: Any) -> Any:
        return None if v in ("", "none", "None") else v

    @field_validator("watch_wallet", "moonshot_wallet", mode="before")
    @classmethod
    def _wallet(cls, v: Any) -> Any:
        v = str(v or "").strip()
        if v and not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError("The wallet must be a 0x… EVM address (42 characters). Never paste a private key.")
        return v.lower()

    def new_trend_coin(self, coin: str, now_ms: int) -> bool:
        """An extra trend-follower coin still inside its half-size period."""
        from datetime import UTC, datetime

        if coin not in self.trend_extra_coins or coin in self.lab_coins or not self.trend_extra_half_until:
            return False
        return datetime.fromtimestamp(now_ms / 1000, UTC).date().isoformat() < self.trend_extra_half_until

    @field_validator("trend_extra_half_until")
    @classmethod
    def _date(cls, v: str) -> str:
        from datetime import date

        v = (v or "").strip()
        if v:
            date.fromisoformat(v)
        return v

    @field_validator("auto_coins", "lab_coins", "trend_extra_coins", mode="before")
    @classmethod
    def _auto_coins(cls, v: Any) -> Any:
        return [x.upper() for x in _csv(v)] if v not in (None, "") else []

    @field_validator("lab_bots", mode="before")
    @classmethod
    def _lab_bots(cls, v: Any) -> Any:
        bots = [x.strip().lower() for x in _csv(v)] if v not in (None, "") else []
        known = ("breakdown_short", "trend_follow", "range_revert", "funding_carry")
        bad = [b for b in bots if b not in known]
        if bad:
            raise ValueError(f"unknown lab bot(s) {bad}; choose from {known}")
        return bots

    @field_validator("database_url")
    @classmethod
    def _db(cls, v: str) -> str:
        # Supabase/Neon/Heroku style URLs -> SQLAlchemy psycopg3 driver.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.trading_timeframe not in VALID_TFS:
            raise ValueError(f"TRADING_TIMEFRAME {self.trading_timeframe!r} invalid")
        if len(self.tf_weights) != len(self.timeframes):
            raise ValueError("TF_WEIGHTS must have one weight per entry in TIMEFRAMES")
        if self.auth_enabled and not (self.ui_password_hash and self.session_secret):
            raise ValueError("AUTH_ENABLED=true requires UI_PASSWORD_HASH and SESSION_SECRET")
        if self.telegram_enabled and not self.telegram_bot_token:
            raise ValueError("TELEGRAM_ENABLED=true requires TELEGRAM_BOT_TOKEN")
        if self.execution_enabled:
            raise ValueError("EXECUTION_ENABLED is a Phase 2 feature and is not implemented")
        return self

    # --- helpers ---
    @property
    def favorite_groups(self) -> list[tuple[str, list[str]]]:
        """Parse DEFAULT_FAVORITES: either 'BTC,ETH' or 'Group:BTC,ETH|Other:SOL'."""
        out: list[tuple[str, list[str]]] = []
        for chunk in self.default_favorites.split("|"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                g, syms = chunk.split(":", 1)
            else:
                g, syms = "Favourites", chunk
            out.append((g.strip(), [s.strip().upper() for s in syms.split(",") if s.strip()]))
        return out

    @property
    def tf_weight_map(self) -> dict[str, float]:
        return dict(zip(self.timeframes, self.tf_weights, strict=True))

    @property
    def group_weights(self) -> dict[str, float]:
        return {
            "trend": self.weights_trend,
            "momentum": self.weights_momentum,
            "structure": self.weights_structure,
            "derivatives": self.weights_derivatives,
            "regime": self.weights_regime,
            "sentiment": self.weights_sentiment,
            "macro": self.weights_macro,
        }

    def masked(self) -> dict[str, Any]:
        d = self.model_dump()
        for k in SECRET_KEYS:
            if d.get(k):
                val = str(d[k])
                d[k] = "••••" + (val[-4:] if len(val) > 8 and k != "database_url" else "")
        return d


@lru_cache
def get_settings() -> Settings:
    return Settings()


def effective_settings(overrides: dict[str, Any] | None) -> Settings:
    """Env settings with DB overrides applied (only RUNTIME_KEYS), re-validated."""
    base = get_settings()
    if not overrides:
        return base
    data = base.model_dump()
    for k, v in overrides.items():
        if k in RUNTIME_KEYS:
            data[k] = v
    return Settings.model_validate(data)
