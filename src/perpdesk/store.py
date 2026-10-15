"""DB helpers: candle/funding/snapshot storage, shared KV cache, settings overrides, health, heartbeats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from perpdesk.config import RUNTIME_KEYS, Settings, effective_settings
from perpdesk.db.models import KV, Candle, CtxSnapshot, FundingRow, JobHeartbeat, SettingOverride, SourceHealthRow
from perpdesk.sources.base import HEALTH
from perpdesk.timeutil import index_ms


def utcnow() -> datetime:
    return datetime.now(UTC)


def _dialect(s: Session) -> str:
    bind = s.get_bind()
    return bind.dialect.name


def _upsert(s: Session, model: Any, rows: list[dict[str, Any]], keys: list[str]) -> None:
    if not rows:
        return
    name = _dialect(s)
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        for i in range(0, len(rows), 1000):
            stmt = pg_insert(model).values(rows[i : i + 1000])
            upd = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
            s.execute(stmt.on_conflict_do_update(index_elements=keys, set_=upd) if upd else
                      stmt.on_conflict_do_nothing(index_elements=keys))
    else:
        from sqlalchemy.dialects.sqlite import insert as sl_insert

        for i in range(0, len(rows), 500):
            sstmt = sl_insert(model).values(rows[i : i + 500])
            supd = {c: sstmt.excluded[c] for c in rows[0] if c not in keys}
            s.execute(sstmt.on_conflict_do_update(index_elements=keys, set_=supd) if supd else
                      sstmt.on_conflict_do_nothing(index_elements=keys))


# ---------------------------------------------------------------- candles


def save_candles(s: Session, series: str, tf: str, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    ts = index_ms(df)
    rows = [
        {"series": series, "tf": tf, "ts": int(t), "o": float(o), "h": float(h), "l": float(lo), "c": float(c),
         "v": float(v)}
        for t, o, h, lo, c, v in zip(ts, df["open"], df["high"], df["low"], df["close"], df["volume"], strict=True)
    ]
    _upsert(s, Candle, rows, ["series", "tf", "ts"])
    return len(rows)


def load_candles(s: Session, series: str, tf: str, since_ms: int | None = None, limit: int | None = None
                 ) -> pd.DataFrame:
    q = select(Candle.ts, Candle.o, Candle.h, Candle.l, Candle.c, Candle.v).where(Candle.series == series,
                                                                                    Candle.tf == tf)
    if since_ms is not None:
        q = q.where(Candle.ts >= since_ms)
    if limit is not None:
        q = q.order_by(Candle.ts.desc()).limit(limit)
    else:
        q = q.order_by(Candle.ts)
    rows = s.execute(q).all()
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).sort_values("ts")
    idx = pd.DatetimeIndex(pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True), name="ts")
    return df[["open", "high", "low", "close", "volume"]].astype(float).set_axis(idx)


def last_candle_ts(s: Session, series: str, tf: str) -> int | None:
    v = s.execute(select(func.max(Candle.ts)).where(Candle.series == series, Candle.tf == tf)).scalar()
    return int(v) if v is not None else None


def prune_candles(s: Session, series: str, tf: str, keep: int) -> None:
    cutoff = s.execute(select(Candle.ts).where(Candle.series == series, Candle.tf == tf)
                       .order_by(Candle.ts.desc()).offset(keep).limit(1)).scalar()
    if cutoff is not None:
        s.execute(delete(Candle).where(Candle.series == series, Candle.tf == tf, Candle.ts <= cutoff))


# ---------------------------------------------------------------- funding / ctx snapshots


def save_funding(s: Session, coin: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    ts = index_ms(df)
    rows = [{"coin": coin, "ts": int(t), "rate": float(r), "premium": float(p)}
            for t, r, p in zip(ts, df["rate"], df["premium"], strict=True)]
    _upsert(s, FundingRow, rows, ["coin", "ts"])


def load_funding(s: Session, coin: str, since_ms: int) -> pd.DataFrame:
    rows = s.execute(select(FundingRow.ts, FundingRow.rate, FundingRow.premium)
                     .where(FundingRow.coin == coin, FundingRow.ts >= since_ms).order_by(FundingRow.ts)).all()
    if not rows:
        return pd.DataFrame(columns=["rate", "premium"], index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = pd.DataFrame(rows, columns=["ts", "rate", "premium"])
    idx = pd.DatetimeIndex(pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True), name="ts")
    return df[["rate", "premium"]].astype(float).set_axis(idx)


def last_funding_ts(s: Session, coin: str) -> int | None:
    v = s.execute(select(func.max(FundingRow.ts)).where(FundingRow.coin == coin)).scalar()
    return int(v) if v is not None else None


def save_ctx_snapshot(s: Session, coin: str, ts_ms: int, mark: float, oracle: float, funding: float, oi: float,
                      premium: float | None, vlm: float) -> None:
    _upsert(s, CtxSnapshot, [{"coin": coin, "ts": ts_ms, "mark": mark, "oracle": oracle, "funding": funding, "oi": oi,
                              "premium": premium, "day_ntl_vlm": vlm}], ["coin", "ts"])


def load_oi(s: Session, coin: str, since_ms: int) -> pd.DataFrame | None:
    rows = s.execute(select(CtxSnapshot.ts, CtxSnapshot.oi, CtxSnapshot.mark)
                     .where(CtxSnapshot.coin == coin, CtxSnapshot.ts >= since_ms).order_by(CtxSnapshot.ts)).all()
    if len(rows) < 3:
        return None
    df = pd.DataFrame(rows, columns=["ts", "oi", "price"])
    idx = pd.DatetimeIndex(pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True), name="ts")
    return df[["oi", "price"]].astype(float).set_axis(idx)


# ---------------------------------------------------------------- KV cache (shared across instances)


def kv_get(s: Session, key: str, max_age: timedelta | None = None) -> Any | None:
    row = s.get(KV, key)
    if row is None:
        return None
    now = utcnow()
    upd = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=UTC)
    if max_age is not None and now - upd > max_age:
        return None
    return row.value.get("v")


def kv_get_with_time(s: Session, key: str) -> tuple[Any, datetime] | None:
    row = s.get(KV, key)
    if row is None:
        return None
    upd = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=UTC)
    return row.value.get("v"), upd


def kv_set(s: Session, key: str, value: Any) -> None:
    row = s.get(KV, key)
    now = utcnow()
    if row is None:
        s.add(KV(key=key, value={"v": value}, updated_at=now, expires_at=None))
    else:
        row.value = {"v": value}
        row.updated_at = now


# ---------------------------------------------------------------- settings overrides


def load_overrides(s: Session) -> dict[str, Any]:
    return {r.key: r.value.get("v") for r in s.execute(select(SettingOverride)).scalars()}


def settings_for(s: Session) -> Settings:
    try:
        return effective_settings(load_overrides(s))
    except Exception:
        return effective_settings(None)


def set_override(s: Session, key: str, value: Any) -> None:
    if key not in RUNTIME_KEYS:
        raise KeyError(key)
    row = s.get(SettingOverride, key)
    if value is None or value == "":
        if row:
            s.delete(row)
        return
    # validate by building effective settings with the new value
    ov = load_overrides(s)
    ov[key] = value
    value = getattr(effective_settings(ov), key)  # validated and coerced to the right type
    if row is None:
        s.add(SettingOverride(key=key, value={"v": value}, updated_at=utcnow()))
    else:
        row.value = {"v": value}
        row.updated_at = utcnow()


def clear_overrides(s: Session) -> None:
    s.execute(delete(SettingOverride))


# ---------------------------------------------------------------- health & heartbeats


def persist_health(s: Session) -> None:
    for name, h in list(HEALTH.items()):
        row = s.get(SourceHealthRow, name)
        if row is None:
            row = SourceHealthRow(name=name, calls=0, errors=0, latency_ms=0)
            s.add(row)
        row.calls = (row.calls or 0) + h.calls
        row.errors = (row.errors or 0) + h.errors
        row.latency_ms = h.latency_ms
        if h.last_success:
            row.last_success = h.last_success
        if h.last_error_at:
            row.last_error, row.last_error_at = h.last_error, h.last_error_at
        h.calls = h.errors = 0


def heartbeat(s: Session, name: str, ok: bool, duration_ms: int, detail: str = "", cursor: int | None = None) -> None:
    row = s.get(JobHeartbeat, name)
    if row is None:
        row = JobHeartbeat(name=name, last_run=utcnow(), ok=ok, duration_ms=duration_ms, detail=detail, cursor=0)
        s.add(row)
    row.last_run, row.ok, row.duration_ms, row.detail = utcnow(), ok, duration_ms, detail[:2000]
    if cursor is not None:
        row.cursor = cursor
