"""Optional plain-English explanation of a plan via the Claude API.

The model receives only computed facts and is told not to introduce numbers. A numeric guard then
rejects any reply containing a number that is not in the facts; in that case (or if the API is off,
over budget or failing) the deterministic explanation is used instead. Results are cached per plan.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.db.models import LlmCache
from perpdesk.llm import usage

SYSTEM = (
    "You explain crypto perpetual-futures trade plans to the trader who will place them by hand. "
    "You receive a JSON object of facts computed by deterministic code. Write 4-6 short sentences in plain "
    "English: what the call is, the main reasons for it, the main risks against it, and how to manage it "
    "(entry choice, stop, taking profit). Rules: do NOT state any number, price, percentage or level that is "
    "not copied exactly from the facts; prefer describing levels in words (\"the stop below the prior day "
    "low\") over repeating numbers; never suggest a different size, leverage, stop or target; never promise "
    "outcomes. No markdown, no bullet points."
)

NUM = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,]*\.?\d*")


def facts_from_view(view: dict[str, Any]) -> dict[str, Any]:
    p = view.get("plan") or {}
    return {
        "asset": view["asset"]["symbol"], "perp": view["asset"]["perp"], "direction": view.get("direction"),
        "grade": view.get("grade"), "setup": view.get("setup_label"), "setup_note": view.get("setup_note"),
        "expected_move": view.get("expected_move"), "confidence": view.get("confidence"),
        "reasons_for": view.get("top_for"), "reasons_against": view.get("top_against"),
        "caution_flags": [f["reason"] for f in view.get("flags", [])],
        "entry_note": p.get("entry_note"), "stop": p.get("sl"), "tp1": p.get("tp1"), "tp2": p.get("tp2"),
        "invalidation": p.get("invalidation"), "leverage": p.get("leverage"), "rr_tp1": round(p.get("rr_tp1") or 0, 2),
        "take_hint": "TAKE" if view.get("take") else "SKIP", "take_reason": view.get("take_reason"),
    }


def deterministic(view: dict[str, Any]) -> str:
    f = facts_from_view(view)
    parts = [f"{f['asset']} {f['direction']} (grade {f['grade']}, {f['setup']})."]
    if f["reasons_for"]:
        parts.append("For: " + "; ".join(f["reasons_for"]) + ".")
    if f["reasons_against"]:
        parts.append("Against: " + "; ".join(f["reasons_against"]) + ".")
    if f["caution_flags"]:
        parts.append("Caution: " + "; ".join(f["caution_flags"]) + ".")
    parts.append(f"{f['entry_note']}. Invalidation: {f['invalidation']}.")
    parts.append("Take half at TP1 and move the stop to breakeven.")
    return " ".join(parts)


def _numbers(text: str) -> set[str]:
    out = set()
    for m in NUM.findall(text):
        t = m.replace(",", "").lstrip("+").rstrip(".")
        if t and t not in {"-", "."}:
            try:
                out.add(f"{float(t):.6g}")
            except ValueError:
                continue
    return out


def guard_ok(reply: str, facts: dict[str, Any]) -> bool:
    allowed = _numbers(json.dumps(facts, ensure_ascii=False)) | {"1", "2", "50"}  # "TP1", "TP2", "50%" split
    return _numbers(reply) <= allowed


async def explain(s: Session, settings: Settings, view: dict[str, Any], plan_id: int | None = None) -> dict[str, Any]:
    facts = facts_from_view(view)
    key = hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()
    cached = s.get(LlmCache, key)
    if cached:
        return {"text": cached.text, "source": f"Claude ({cached.model}), cached"}
    if not settings.anthropic_api_key:
        return {"text": deterministic(view), "source": "built-in (no ANTHROPIC_API_KEY)"}
    if usage.budget_exhausted(s, settings):
        return {"text": deterministic(view), "source": "built-in (monthly LLM budget reached)"}
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=30.0, max_retries=1)
    model = settings.llm_model
    try:
        resp = await client.beta.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True)}],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.RateLimitError as e:
        return {"text": deterministic(view), "source": f"built-in (Claude rate-limited: {e.status_code})"}
    except anthropic.APIStatusError as e:
        return {"text": deterministic(view), "source": f"built-in (Claude API error {e.status_code})"}
    except anthropic.APIConnectionError:
        return {"text": deterministic(view), "source": "built-in (Claude API unreachable)"}
    served = getattr(resp, "model", model) or model
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
    ok = resp.stop_reason != "refusal" and bool(text) and guard_ok(text, facts)
    usage.record(s, served, "explain", resp.usage, plan_id, ok)
    if not ok:
        why = "refused" if resp.stop_reason == "refusal" else "reply contained numbers not in the plan"
        return {"text": deterministic(view), "source": f"built-in (Claude {why}; discarded)"}
    s.add(LlmCache(key=key, text=text, model=served))
    return {"text": text, "source": f"Claude ({served})"}
