"""Telegram: alerts out; commands in (`/status`, `/check SOL`, and the auto-trading switch `/autostart` `/autostop`
`/autokill yes`, handled in web/routes/api.py).

The bot only talks to one chat: the one linked from the Settings page (a one-time code sent to the bot), or
TELEGRAM_CHAT_ID / TELEGRAM_ALLOWED_USER_IDS from the environment. The token lives only in the hosting env."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.db.models import AlertState
from perpdesk.plan import Plan
from perpdesk.sources.base import request_json

LINK_KEY = "telegram:link"
CODE_KEY = "telegram:link_code"
CODE_TTL = timedelta(minutes=15)


def configured(settings: Settings) -> bool:
    return bool(settings.telegram_bot_token)


def webhook_secret(settings: Settings) -> str:
    if settings.telegram_webhook_secret:
        return settings.telegram_webhook_secret
    if not settings.telegram_bot_token:
        return ""
    return hmac.new(settings.telegram_bot_token.encode(), b"perpdesk-webhook", hashlib.sha256).hexdigest()[:40]


def linked(s: Session) -> dict[str, Any] | None:
    from perpdesk.store import kv_get

    v = kv_get(s, LINK_KEY)
    return v if isinstance(v, dict) and v.get("chat_id") else None


def chat_id(s: Session, settings: Settings) -> str | int | None:
    if not configured(settings):
        return None
    ln = linked(s)
    return ln["chat_id"] if ln else (settings.telegram_chat_id or None)


def allowed_user(s: Session, settings: Settings, uid: Any) -> bool:
    ln = linked(s)
    return uid is not None and (uid in settings.telegram_allowed_user_ids or (ln is not None and ln.get("user_id") == uid))


def new_link_code(s: Session) -> str:
    from perpdesk.store import kv_set

    code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    kv_set(s, CODE_KEY, {"code": code, "expires": (datetime.now(UTC) + CODE_TTL).isoformat()})
    return code


def try_link(s: Session, text: str, chat: Any, uid: Any) -> bool:
    """`/start <code>` from the deep link (or the code alone) links this chat if the code is current."""
    from perpdesk.store import kv_get, kv_set

    want = kv_get(s, CODE_KEY)
    parts = text.split()
    given = parts[-1] if parts else ""
    if not (isinstance(want, dict) and want.get("code") and given):
        return False
    if datetime.fromisoformat(want["expires"]) < datetime.now(UTC) or not hmac.compare_digest(given, want["code"]):
        return False
    kv_set(s, LINK_KEY, {"chat_id": chat, "user_id": uid, "linked_at": datetime.now(UTC).isoformat()})
    kv_set(s, CODE_KEY, None)
    return True


def unlink(s: Session) -> None:
    from perpdesk.store import kv_set

    kv_set(s, LINK_KEY, None)


async def api(settings: Settings, method: str, payload: dict[str, Any] | None = None) -> Any:
    return await request_json("telegram", "POST", f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
                              json=payload or {}, retries=1)


async def send(settings: Settings, chat: str | int, text: str, html: bool = False) -> None:
    """html=True: Telegram HTML (<b>, <a>); callers escape any free text they insert."""
    body: dict[str, Any] = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}
    if html:
        body["parse_mode"] = "HTML"
    await api(settings, "sendMessage", body)


async def notify(s: Session, settings: Settings, text: str, html: bool = False) -> bool:
    """Send to the linked chat. Never raises: alerts must not break the scheduler."""
    chat = chat_id(s, settings)
    if chat is None:
        return False
    try:
        await send(settings, chat, text, html=html)
        return True
    except Exception:
        return False


async def maybe_alert(s: Session, settings: Settings, plan: Plan, view: dict[str, Any]) -> None:
    """Old alert: general-analysis direction flips. Off by default (ALERTS_GENERAL_FLIPS): those signals lost money."""
    if not (settings.alerts_general_flips and chat_id(s, settings)):
        return
    coin = plan.coin
    st = s.get(AlertState, coin)
    msg = None
    if st is None:
        s.add(AlertState(coin=coin, direction=plan.direction, grade=plan.grade, updated_at=datetime.now(UTC)))
        return
    if st.direction != plan.direction:
        msg = f"🔄 {coin} general analysis flipped to {plan.direction} (grade {plan.grade}) — SKIP signal, context only"
    elif plan.grade == "A" and st.grade != "A":
        msg = f"⭐ {coin} {plan.direction} upgraded to grade A"
    st.direction, st.grade, st.updated_at = plan.direction, plan.grade, datetime.now(UTC)
    if msg:
        await notify(s, settings, msg + "\n\n" + plan.copy_text() + "\n\nSignals are probabilistic. Not financial advice.")


def format_check(view: dict[str, Any]) -> str:
    if view.get("error"):
        return f"{view['asset']['symbol']}: unavailable — {view['error']}"
    p = view["plan"]
    return (str(p["copy"]) + f"\nUpdated {view['updated_at'][:16]} UTC\nThis is the general analysis (a SKIP signal "
            "unless a proven bot fires). Not financial advice.")
