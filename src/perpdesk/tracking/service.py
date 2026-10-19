"""DB-backed lookups used when building plans and cards: gate status, win probability, live stats,
TAKE/SKIP hints, calibration warnings."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from perpdesk.analysis.setups import SETUP_LABELS
from perpdesk.analysis.types import Flag
from perpdesk.config import Settings
from perpdesk.db.models import BacktestRun, PlanOutcome, PlanRow, SetupStat
from perpdesk.plan import GateInfo, WinStats
from perpdesk.tracking.calibration import Calibrator, reliability
from perpdesk.tracking.stats import r_stats


def latest_run_id(s: Session) -> int | None:
    return s.execute(select(func.max(BacktestRun.id))).scalar()


def latest_score_quantiles(s: Session) -> tuple[float, ...]:
    run = latest_run_id(s)
    if run is None:
        return ()
    row = s.get(BacktestRun, run)
    q = (row.summary or {}).get("score_quantiles") if row else None
    return tuple(float(x) for x in q) if q and len(q) >= 2 else ()


def _proof(row: SetupStat) -> str:
    st = row.stats
    pf = st.get("profit_factor")
    pf_s = f"{pf:.2f}" if isinstance(pf, int | float) and pf != float("inf") else "∞" if pf else "–"
    return (f"{SETUP_LABELS.get(row.setup, row.setup)} · {row.scope} · {row.direction} — {st.get('n', 0)} OOS trades, "
            f"win {st.get('win_rate', 0):.0%}, avg {st.get('avg_r', 0):+.2f}R, PF {pf_s}, max DD {st.get('max_dd_r', 0):.0f}R")


def live_rs(s: Session, **filters: object) -> list[float]:
    q = select(PlanOutcome.r_multiple).join(PlanRow, PlanRow.id == PlanOutcome.plan_id).where(
        PlanOutcome.filled.is_(True), PlanOutcome.r_multiple.is_not(None))
    for k, v in filters.items():
        q = q.where(getattr(PlanRow, k) == v)
    q = q.order_by(PlanOutcome.exit_ms)
    return [float(x) for x in s.execute(q).scalars() if x is not None]


def gate_info(s: Session, settings: Settings, setup: str, coin: str, cls: str, direction: str, confidence: int
              ) -> GateInfo:
    run = latest_run_id(s)
    if run is None:
        return GateInfo(False, None, None, None, None)
    rows = s.execute(select(SetupStat).where(SetupStat.run_id == run, SetupStat.setup == setup,
                                             SetupStat.direction == direction)).scalars().all()
    by_scope = {(r.scope_kind, r.scope): r for r in rows}
    ordered = [by_scope.get(("asset", coin)), by_scope.get(("class", cls)), by_scope.get(("all", "*"))]
    present = [r for r in ordered if r is not None]
    if not present:
        return GateInfo(False, None, None, None, None)
    chosen = next((r for r in present if int(r.stats.get("n", 0)) >= settings.min_sample_for_stats),
                  max(present, key=lambda r: int(r.stats.get("n", 0))))
    gate_rows = [r for r in ordered[:2] if r is not None]
    passed = setup != "composite" and any(r.gate_passed and not r.live_demoted for r in gate_rows)
    demoted = next((r.live_reason for r in gate_rows if r.gate_passed and r.live_demoted), None)
    win: WinStats | None = None
    n = int(chosen.stats.get("n", 0))
    if chosen.calibration:
        cal = Calibrator.from_json(chosen.calibration)
        p, lo, hi, _ = cal.predict(confidence)
        win = WinStats(p=p, lo=lo, hi=hi, n=n, scope=f"{SETUP_LABELS.get(setup, setup)} · {chosen.scope} · {direction}",
                       low_sample=n < settings.min_sample_for_stats,
                       expectancy_r=float(chosen.stats.get("avg_r", 0.0)),
                       pf=chosen.stats.get("profit_factor"),
                       win_rate=float(chosen.stats.get("win_rate", 0.0)),
                       max_dd_r=float(chosen.stats.get("max_dd_r", 0.0)))
    lr = live_rs(s, setup=setup, coin=coin, direction=direction)
    live = None
    if lr:
        ls = r_stats(lr)
        live = f"Live: {ls.n} trades, win {ls.win_rate:.0%}, avg {ls.avg_r:+.2f}R"
    return GateInfo(passed=passed, demoted_reason=demoted, stats=win, proof=_proof(chosen), live_proof=live)


@dataclass
class TakeHint:
    take: bool
    reason: str


def take_hint(s: Session, settings: Settings, grade: str) -> TakeHint:
    """TAKE for the general (composite) plan needs either a setup that passed every backtest gate (grade A) or a
    live record of take_min_live_trades+ trades whose average R is positive and statistically solid (t > 2).
    A few dozen lucky trades are not enough: that rule once flipped every grade C card to TAKE."""
    if grade == "A":
        return TakeHint(True, "grade A: setup passed every statistical gate")
    rs = live_rs(s, grade=grade)
    need = settings.take_min_live_trades
    if len(rs) < need:
        return TakeHint(False, f"grade {grade} has {len(rs)}/{need} live trades — not proven yet")
    st = r_stats(rs)
    t_stat = _t_stat(rs)
    if st.avg_r > 0 and t_stat > 2:
        return TakeHint(True, f"grade {grade} live: {st.avg_r:+.2f}R avg over {st.n} trades (t={t_stat:.1f})")
    return TakeHint(False, f"grade {grade} live: {st.avg_r:+.2f}R avg over {st.n} trades (t={t_stat:.1f}), "
                           "no proven edge")


def _t_stat(rs: list[float]) -> float:
    n = len(rs)
    if n < 2:
        return 0.0
    m = sum(rs) / n
    var = sum((x - m) ** 2 for x in rs) / (n - 1)
    return m / (var ** 0.5 / n ** 0.5) if var > 0 else (float("inf") if m > 0 else 0.0)


def live_warnings(s: Session, settings: Settings, coin: str, confidence: int) -> list[str]:
    out = []
    rs = live_rs(s, coin=coin)
    if len(rs) >= settings.min_sample_for_stats:
        st = r_stats(rs)
        if st.avg_r < 0:
            out.append(f"{coin} live expectancy is negative: {st.avg_r:+.2f}R over {st.n} signals")
    bucket = (confidence // 10) * 10
    q = (select(PlanOutcome.r_multiple).join(PlanRow, PlanRow.id == PlanOutcome.plan_id)
         .where(PlanOutcome.filled.is_(True), PlanOutcome.r_multiple.is_not(None), PlanRow.confidence >= bucket,
                PlanRow.confidence < bucket + 10))
    br = [float(x) for x in s.execute(q).scalars() if x is not None]
    if len(br) >= settings.min_sample_for_stats:
        st = r_stats(br)
        if st.avg_r < 0:
            out.append(f"confidence {bucket}–{bucket + 9} calls are losing live: {st.avg_r:+.2f}R over {st.n}")
    return out


def calibration_status(s: Session, settings: Settings) -> tuple[list[dict[str, float]], float, int]:
    q = (select(PlanRow.win_prob, PlanOutcome.tp1_hit).join(PlanOutcome, PlanOutcome.plan_id == PlanRow.id)
         .where(PlanOutcome.filled.is_(True), PlanRow.win_prob.is_not(None),
                PlanOutcome.exit_reason.in_(["sl", "tp2", "be", "timeout"])))
    rows = s.execute(q).all()
    buckets, err = reliability([float(r[0] or 0.0) for r in rows], [bool(r[1]) for r in rows])
    return buckets, err, len(rows)


def calibration_flag(s: Session, settings: Settings) -> Flag | None:
    _, err, n = calibration_status(s, settings)
    if n >= settings.min_sample_for_stats and err > settings.max_calibration_error:
        return Flag("calibration", f"win-probability calibration is off by {err:.0%} on {n} live trades "
                                   f"(limit {settings.max_calibration_error:.0%})", reduces_size=False)
    return None
