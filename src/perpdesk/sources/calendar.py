"""High-impact economic events (ForexFactory weekly JSON feed; unofficial and best-effort)."""

from __future__ import annotations

from datetime import datetime

from perpdesk.analysis.types import EventInfo
from perpdesk.sources.base import request_json

NAME = "forexfactory"
URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


async def high_impact(countries: tuple[str, ...] = ("USD",)) -> list[EventInfo]:
    rows = await request_json(NAME, "GET", URL, retries=1)
    out = []
    for e in rows:
        if e.get("impact") != "High" or e.get("country") not in countries:
            continue
        try:
            at = datetime.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        out.append(EventInfo(e.get("title", ""), at, e.get("impact", ""), e.get("country", "")))
    return sorted(out, key=lambda x: x.at)
