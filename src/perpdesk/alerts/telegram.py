"""Optional read-only Telegram alerts (Phase 1): direction flips and upgrades to grade A, plus a
`/check SOL` command handled through the webhook endpoint. No trading commands exist."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.db.models import AlertState
from perpdesk.plan import Plan
from perpdesk.sources.base import request_json


async def send(settings: Settings, chat_id: str | int, text: str) -> None:
    await request_json("telegram", "POST", f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                       json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, retries=1)


async def maybe_alert(s: Session, settings: Settings, plan: Plan, view: dict[str, Any]) -> None:
    if not (settings.telegram_enabled and settings.telegram_chat_id):
        return
    coin = plan.coin
    st = s.get(AlertState, coin)
    msg = None
    if st is None:
        s.add(AlertState(coin=coin, direction=plan.direction, grade=plan.grade, updated_at=datetime.now(UTC)))
        return
    if st.direction != plan.direction:
        msg = f"🔄 {coin} flipped to {plan.direction} (grade {plan.grade})"
    elif plan.grade == "A" and st.grade != "A":
        msg = f"⭐ {coin} {plan.direction} upgraded to grade A"
    st.direction, st.grade, st.updated_at = plan.direction, plan.grade, datetime.now(UTC)
    if msg:
        try:
            await send(settings, settings.telegram_chat_id, msg + "\n\n" + plan.copy_text() +
                       "\n\nSignals are probabilistic. Not financial advice.")
        except Exception:
            pass


def format_check(view: dict[str, Any]) -> str:
    if view.get("error"):
        return f"{view['asset']['symbol']}: unavailable — {view['error']}"
    p = view["plan"]
    return str(p["copy"]) + f"\nUpdated {view['updated_at'][:16]} UTC\nSignals are probabilistic. Not financial advice."
