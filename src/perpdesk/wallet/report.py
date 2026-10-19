"""Data for the My trades page: live positions with advice, closed history, and what the history says
about trading with vs against the app's signals."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.db.models import Asset, LatestView, WalletTrade
from perpdesk.wallet.advice import Advice, _f, advise, correlation
from perpdesk.wallet.sync import load_state, positions_from_state


def _agg(rows: list[WalletTrade]) -> dict[str, Any]:
    n = len(rows)
    wins = [r for r in rows if r.net_pnl > 0]
    rs = [r.r_multiple for r in rows if r.r_multiple is not None]
    gross_win = sum(r.net_pnl for r in wins)
    gross_loss = -sum(r.net_pnl for r in rows if r.net_pnl <= 0)
    return {
        "n": n, "wins": len(wins), "win_rate": len(wins) / n if n else 0.0,
        "net": sum(r.net_pnl for r in rows), "fees": sum(r.fees for r in rows),
        "builder_fees": sum(r.builder_fees for r in rows), "funding": sum(r.funding for r in rows),
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": gross_loss / (n - len(wins)) if n - len(wins) else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_r": sum(rs) / len(rs) if rs else None, "n_r": len(rs),
    }


def _views_by_coin(s: Session) -> dict[str, tuple[Asset, dict[str, Any]]]:
    out: dict[str, tuple[Asset, dict[str, Any]]] = {}
    for a, v in s.execute(select(Asset, LatestView).join(LatestView, LatestView.asset_id == Asset.id)).all():
        if a.hl_coin:
            out[a.hl_coin] = (a, v.payload)
    return out


def positions_view(s: Session, settings: Settings) -> dict[str, Any]:
    address = settings.watch_wallet
    state, at = load_state(s, address) if address else (None, None)
    views = _views_by_coin(s)
    open_rows = {r.coin: r for r in s.execute(select(WalletTrade).where(WalletTrade.address == address,
                                                                        WalletTrade.status == "open")).scalars()}
    order = {"danger": 0, "warn": 1, "info": 2, "ok": 3}
    positions: list[dict[str, Any]] = []
    upnl, actions = 0.0, 0
    pos_list = positions_from_state(state)
    corr = correlation(pos_list)
    for p in pos_list:
        a, v = views.get(p.coin, (None, None))
        adv = advise(p, v, settings.risk_pct_of_margin)
        adv += [Advice("warn", c.text) for c in corr if p.coin in c.coins]
        adv = sorted(adv, key=lambda x: order.get(x.level, 9))
        upnl += p.upnl or 0.0
        actions += sum(1 for x in adv if x.level in ("danger", "warn"))
        positions.append({"p": p.as_dict(), "advice": adv, "worst": adv[0].level if adv else "ok", "asset": a,
                          "view": v if v and not v.get("error") else None, "trade": open_rows.get(p.coin)})
    positions.sort(key=lambda x: order.get(str(x["worst"]), 9))
    ms = ((state or {}).get("clearinghouse") or {}).get("marginSummary") or {}
    return {"address": address, "positions": positions, "synced_at": at,
            "open_pnl": upnl, "actions": actions, "correlation": [c.text for c in corr],
            "account_value": _f(ms.get("accountValue")),
            "withdrawable": _f(((state or {}).get("clearinghouse") or {}).get("withdrawable"))}


def history_view(s: Session, settings: Settings, limit: int = 200) -> dict[str, Any]:
    address = settings.watch_wallet
    closed = list(s.execute(select(WalletTrade).where(WalletTrade.address == address, WalletTrade.status == "closed")
                            .order_by(WalletTrade.closed_ms.desc())).scalars())
    by_coin: dict[str, list[WalletTrade]] = {}
    by_grade: dict[str, list[WalletTrade]] = {}
    for r in closed:
        by_coin.setdefault(r.coin, []).append(r)
        if r.followed:
            by_grade.setdefault(r.plan_grade or "?", []).append(r)
    return {
        "closed": closed[:limit],
        "overall": _agg(closed),
        "followed": _agg([r for r in closed if r.followed]),
        "against": _agg([r for r in closed if r.followed is False]),
        "no_plan": _agg([r for r in closed if r.followed is None]),
        "by_coin": sorted(((k, _agg(v)) for k, v in by_coin.items()), key=lambda kv: -kv[1]["n"]),
        "by_grade": sorted((k, _agg(v)) for k, v in by_grade.items()),
        "min_sample": settings.min_sample_for_stats,
    }
