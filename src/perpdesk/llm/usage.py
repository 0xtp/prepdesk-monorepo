"""Claude API usage ledger: every call's tokens priced from a model table; spend summaries; balance.

Anthropic has no API that returns remaining prepaid credit. Balance = LLM_CREDIT_USD (what you
loaded, entered in Settings) minus spend recorded since LLM_CREDIT_SINCE. Optionally, with an Admin
API key (organisation accounts only), the daily job pulls the org's billed cost report to compare.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.db.models import LlmUsage
from perpdesk.sources.base import request_json
from perpdesk.store import kv_get_with_time, kv_set

# USD per 1M tokens: input, output, cache read, cache write (5-minute). Prompts <= 100K tokens.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
}


def price_of(model: str) -> tuple[float, float, float, float] | None:
    if model in PRICES:
        return PRICES[model]
    for k, v in PRICES.items():
        if model.startswith(k):
            return v
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    p = price_of(model)
    if p is None:
        return 0.0
    return (input_tokens * p[0] + output_tokens * p[1] + cache_read * p[2] + cache_write * p[3]) / 1_000_000


def record(s: Session, model: str, purpose: str, usage: Any, plan_id: int | None = None, ok: bool = True) -> LlmUsage:
    it = int(getattr(usage, "input_tokens", 0) or 0)
    ot = int(getattr(usage, "output_tokens", 0) or 0)
    cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    row = LlmUsage(ts=datetime.now(UTC), model=model, purpose=purpose, plan_id=plan_id, input_tokens=it,
                   output_tokens=ot, cache_read_tokens=cr, cache_write_tokens=cw, cost_usd=cost_usd(model, it, ot, cr, cw),
                   ok=ok)
    s.add(row)
    return row


def _sum(s: Session, since: datetime | None) -> dict[str, float]:
    q = select(func.count(LlmUsage.id), func.coalesce(func.sum(LlmUsage.cost_usd), 0.0),
               func.coalesce(func.sum(LlmUsage.input_tokens), 0), func.coalesce(func.sum(LlmUsage.output_tokens), 0))
    if since is not None:
        q = q.where(LlmUsage.ts >= since)
    n, cost, it, ot = s.execute(q).one()
    return {"calls": int(n), "cost": float(cost), "input_tokens": int(it), "output_tokens": int(ot)}


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def summary(s: Session, settings: Settings) -> dict[str, Any]:
    now = datetime.now(UTC)
    today = datetime(now.year, now.month, now.day, tzinfo=UTC)
    out: dict[str, Any] = {
        "today": _sum(s, today),
        "month": _sum(s, month_start(now)),
        "all": _sum(s, None),
        "model": settings.llm_model,
        "price": price_of(settings.llm_model),
        "enabled": bool(settings.anthropic_api_key),
    }
    by_model = s.execute(select(LlmUsage.model, func.count(LlmUsage.id), func.sum(LlmUsage.cost_usd))
                         .group_by(LlmUsage.model)).all()
    out["by_model"] = [{"model": m, "calls": int(n), "cost": float(c or 0)} for m, n, c in by_model]
    since = settings.llm_credit_since
    if settings.llm_credit_usd is not None:
        start = datetime(since.year, since.month, since.day, tzinfo=UTC) if isinstance(since, date) else None
        spent = _sum(s, start)["cost"]
        out["balance"] = {"loaded": settings.llm_credit_usd, "since": since.isoformat() if since else "all time",
                          "spent": spent, "remaining": settings.llm_credit_usd - spent}
    if settings.llm_monthly_budget_usd is not None:
        m = out["month"]["cost"]
        out["budget"] = {"limit": settings.llm_monthly_budget_usd, "used": m,
                         "exhausted": m >= settings.llm_monthly_budget_usd}
    hit = kv_get_with_time(s, "anthropic_org_cost")
    if hit is not None:
        out["org_cost"] = {**hit[0], "fetched_at": hit[1].isoformat()}
    recent = s.execute(select(LlmUsage).order_by(LlmUsage.ts.desc()).limit(20)).scalars().all()
    out["recent"] = [{"ts": r.ts.isoformat(), "model": r.model, "purpose": r.purpose, "in": r.input_tokens,
                      "out": r.output_tokens, "cost": r.cost_usd, "ok": r.ok} for r in recent]
    return out


def budget_exhausted(s: Session, settings: Settings) -> bool:
    if settings.llm_monthly_budget_usd is None:
        return False
    return _sum(s, month_start())["cost"] >= settings.llm_monthly_budget_usd


async def sync_org_cost(s: Session, settings: Settings) -> dict[str, Any]:
    """Month-to-date billed cost for the whole Anthropic organisation (Admin API key required)."""
    now = datetime.now(UTC)
    start = month_start(now)
    params: dict[str, Any] = {"starting_at": start.strftime("%Y-%m-%dT00:00:00Z"),
                              "ending_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z"), "limit": 31}
    total_cents = 0.0
    page = None
    for _ in range(10):
        if page:
            params["page"] = page
        d = await request_json("anthropic-admin", "GET", "https://api.anthropic.com/v1/organizations/cost_report",
                               params=params, headers={"x-api-key": settings.anthropic_admin_key,
                                                       "anthropic-version": "2023-06-01"}, retries=1)
        for bucket in d.get("data", []):
            for r in bucket.get("results", []):
                amt = r.get("amount")
                if isinstance(amt, dict):
                    amt = amt.get("value") or amt.get("amount")
                try:
                    total_cents += float(amt or 0)
                except (TypeError, ValueError):
                    continue
        if not d.get("has_more"):
            break
        page = d.get("next_page")
    out = {"month_to_date_usd": total_cents / 100, "month": start.strftime("%Y-%m")}
    kv_set(s, "anthropic_org_cost", out)
    return out
