"""Command line: perpdesk tick | daily | backtest | verify | migrate | serve."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path


def _tick(args: argparse.Namespace) -> int:
    from perpdesk.db.session import migrate, session_scope
    from perpdesk.jobs.tick import tick
    from perpdesk.sources.base import close_client

    migrate()

    async def run() -> dict[str, object]:
        try:
            with session_scope() as s:
                return await tick(s, budget_s=args.budget)
        finally:
            await close_client()

    print(json.dumps(asyncio.run(run()), indent=1, default=str))
    return 0


def _daily(args: argparse.Namespace) -> int:
    from perpdesk.db.session import migrate, session_scope
    from perpdesk.jobs.tick import daily
    from perpdesk.sources.base import close_client

    migrate()

    async def run() -> dict[str, object]:
        try:
            with session_scope() as s:
                return await daily(s)
        finally:
            await close_client()

    print(json.dumps(asyncio.run(run()), indent=1, default=str))
    return 0


def _lab(args: argparse.Namespace) -> int:
    from perpdesk.lab.backtest import run_lab

    end = date.fromisoformat(args.end) if args.end else datetime.now(UTC).date()
    run_lab([c.strip() for c in args.coins.split(",") if c.strip()], date.fromisoformat(args.start), end,
            Path(args.cache), Path(args.out))
    return 0


def _backtest(args: argparse.Namespace) -> int:
    from perpdesk.backtest.main import run_backtest
    from perpdesk.config import get_settings

    end = date.fromisoformat(args.end) if args.end else datetime.now(UTC).date()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=int(args.days))
    st = get_settings()
    classes = {sym: g for g, syms in st.favorite_groups for sym in syms}
    res = run_backtest([c.strip() for c in args.coins.split(",") if c.strip()], start, end, st, Path(args.cache),
                       Path(args.out), classes=classes, do_ablation=args.ablation, write_db=args.db)
    print(json.dumps(res["summary"], indent=1, default=str))
    return 0


def _verify(args: argparse.Namespace) -> int:
    """Live proof: real prices/funding for BTC & SOL side by side, search checks, and full example plans."""
    import os
    import tempfile

    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/verify.db"
    from perpdesk.config import get_settings

    get_settings.cache_clear()
    from perpdesk.assets.favorites import add_favorite
    from perpdesk.assets.registry import search, universe
    from perpdesk.db.session import migrate, session_scope
    from perpdesk.pipeline import analyse_asset, load_context
    from perpdesk.sources.base import close_client
    from perpdesk.sources.hyperliquid import Hyperliquid

    migrate()
    st = get_settings()
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]

    async def run() -> int:
        hl = Hyperliquid(st.hyperliquid_api_url)
        try:
            u = await universe(hl)
            print(f"== Hyperliquid live ({datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC) — compare with app.hyperliquid.xyz")
            print(f"{'coin':6s} {'mark':>12s} {'oracle':>12s} {'funding/h %':>12s} {'8h %':>9s} {'APR %':>8s} {'maxLev':>6s}")
            for c in ["BTC", "SOL", *[x for x in coins if x not in ("BTC", "SOL")]]:
                x = u.ctxs.get(c)
                if x:
                    print(f"{c:6s} {x.mark_px:>12,.6g} {x.oracle_px:>12,.6g} {x.funding * 100:>12.5f} "
                          f"{x.funding * 800:>9.4f} {x.funding * 24 * 365 * 100:>8.2f} {u.perps[c].max_leverage:>6d}")
            print("\n== Search checks")
            for q in args.search:
                res = await search(q, hl, st.coingecko_api_key)
                top = "; ".join(f"{r.symbol} [{'perp' if r.perp else 'spot only'}] {r.key}" for r in res[:3])
                print(f"  {q!r:>48s} -> {top or 'no results'}")
            with session_scope() as s:
                ctx = await load_context(s, hl, st)
                print("\n== Context sources")
                for k, v in ctx.status.items():
                    print(f"  {k:24s} {'ok' if v.get('ok') else 'UNAVAILABLE'} {v.get('error') or ''}")
                keys = list(coins) + ([args.spot] if args.spot else [])
                for key in keys:
                    sr = await search(key, hl, st.coingecko_api_key) if key.startswith("0x") or len(key) > 30 else []
                    real_key = sr[0].key if sr else key
                    a = await add_favorite(s, real_key, hl)
                    s.commit()
                    ar = await analyse_asset(s, hl, a, ctx, st, log=True)
                    s.commit()
                    print(f"\n== {a.symbol} ({'perp' if a.perp_available and a.hl_coin else 'SPOT VIEW ONLY, no perp market'})")
                    if ar.error:
                        print(f"  unavailable: {ar.error}")
                        continue
                    v, p = ar.view, ar.plan
                    assert p is not None and ar.analysis is not None
                    print(p.copy_text())
                    w = f"{p.win.p:.0%} ({p.win.lo:.0%}–{p.win.hi:.0%}, n={p.win.n})" if p.win else "n/a (no backtest yet)"
                    print(f"  setup {p.setup_label} · confidence {p.confidence} · expected move {p.expected_move} · "
                          f"win {w} · break-even {p.break_even:.0%} · {'TAKE' if v['take'] else 'SKIP'} ({v['take_reason']})")
                    print(f"  notional ${p.notional:,.2f} · qty {p.qty} · margin ${p.margin_used:,.2f} · fees ${p.fees_usd:.3f}"
                          f" · $ at TP1 {p.usd_tp1:.2f} · $ if TP2 {p.usd_tp2_total:.2f} · liq buffer "
                          f"{(p.liq_buffer_atr or 0):.1f} ATR")
                    for f in p.flags:
                        print(f"  ⚠ {f.reason}")
                    print("  top for: " + " | ".join(v["top_for"]))
                    print("  top against: " + " | ".join(v["top_against"]))
                    if args.factors:
                        for fac in ar.analysis.factors:
                            info = "(info)" if not fac.scored else ""
                            print(f"    {fac.group:11s} {fac.tf:4s} {fac.name:24s} {fac.score:+.2f} {info} {fac.reason}")
        finally:
            await close_client()
        return 0

    return asyncio.run(run())


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("perpdesk.web.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="perpdesk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick", help="score plans, refresh and analyse favourites")
    t.add_argument("--budget", type=float, default=240)
    t.set_defaults(fn=_tick)
    sub.add_parser("daily", help="daily maintenance + live gate").set_defaults(fn=_daily)
    b = sub.add_parser("backtest", help="walk-forward backtest")
    b.add_argument("--coins", default="SOL,BTC")
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--days", default="730")
    b.add_argument("--cache", default=".cache/binance")
    b.add_argument("--out", default="reports")
    b.add_argument("--ablation", action="store_true")
    b.add_argument("--db", action="store_true", help="write results to DATABASE_URL")
    b.set_defaults(fn=_backtest)
    lb = sub.add_parser("lab", help="strategy-lab backtest (all lab bots, fixed settings)")
    lb.add_argument("--coins", default="SOL,BTC,ETH,HYPE,AVAX,LINK,SUI,UNI,DOGE,ENA,ONDO,ZEC")
    lb.add_argument("--start", default="2021-01-01")
    lb.add_argument("--end")
    lb.add_argument("--cache", default=".cache/binance")
    lb.add_argument("--out", default="reports")
    lb.set_defaults(fn=_lab)
    v = sub.add_parser("verify", help="live proof run against the real APIs")
    v.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    v.add_argument("--spot", default="", help="a contract address to check the spot-only path")
    v.add_argument("--search", nargs="*", default=["SOL", "solana", "pengu"])
    v.add_argument("--factors", action="store_true")
    v.set_defaults(fn=_verify)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(
        fn=lambda a: (__import__("perpdesk.db.session", fromlist=["migrate"]).migrate(), 0)[1])
    sv = sub.add_parser("serve", help="run the web app locally")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(fn=_serve)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
