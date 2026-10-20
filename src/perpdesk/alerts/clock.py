"""Local time for alerts: an IANA zone name when the server has the tz database, or a fixed UTC offset."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo

_OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.I)


def parse_tz(name: str) -> tzinfo | None:
    name = (name or "").strip()
    if name.upper() in ("", "UTC", "GMT", "Z"):
        return UTC
    m = _OFFSET.match(name)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if h > 14 or mi > 59:
            return None
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=h, minutes=mi))
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def local(ms: int, tz_name: str) -> datetime:
    return datetime.fromtimestamp(ms / 1000, parse_tz(tz_name) or UTC)


def hhmm(ms: int, tz_name: str) -> str:
    d = local(ms, tz_name)
    label = "UTC" if (parse_tz(tz_name) or UTC) == UTC else tz_name
    return f"{d:%H:%M} {label}"


def in_quiet_hours(ms: int, tz_name: str, quiet: str) -> bool:
    if not quiet:
        return False
    a, b = (int(x) for x in quiet.split("-"))
    h = local(ms, tz_name).hour
    return (a <= h < b) if a <= b else (h >= a or h < b)
