"""Shared HTTP plumbing for data sources: results with status, retries with backoff, rate limiting,
TTL caching and per-source health tracking. Sources never raise into the UI: they return
SourceResult(ok=False, error=...) and the UI shows "unavailable" with the error and timestamp."""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

import httpx

T = TypeVar("T")
USER_AGENT = "PerpDesk/0.1 (+https://github.com/0xtp/prepdesk-monorepo)"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SourceResult(Generic[T]):
    source: str
    ok: bool
    data: T | None = None
    error: str | None = None
    fetched_at: datetime = field(default_factory=utcnow)
    latency_ms: int = 0
    cached: bool = False

    def unwrap(self) -> T:
        if not self.ok or self.data is None:
            raise SourceError(f"{self.source}: {self.error}")
        return self.data


class SourceError(RuntimeError):
    pass


@dataclass
class SourceHealth:
    name: str
    last_success: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    latency_ms: int = 0
    calls: int = 0
    errors: int = 0


HEALTH: dict[str, SourceHealth] = {}


def record(name: str, ok: bool, latency_ms: int, error: str | None = None) -> None:
    h = HEALTH.setdefault(name, SourceHealth(name))
    h.calls += 1
    h.latency_ms = latency_ms
    if ok:
        h.last_success = utcnow()
    else:
        h.errors += 1
        h.last_error = error
        h.last_error_at = utcnow()


class TokenBucket:
    """Async token bucket. capacity tokens refill evenly over 60 seconds."""

    def __init__(self, per_minute: float) -> None:
        self.capacity = per_minute
        self.tokens = per_minute
        self.rate = per_minute / 60.0
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: float) -> None:
        weight = min(weight, self.capacity)
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= weight:
                    self.tokens -= weight
                    return
                await asyncio.sleep((weight - self.tokens) / self.rate)


class TTLCache:
    def __init__(self) -> None:
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        v = self._d.get(key)
        if v is None:
            return None
        exp, data = v
        if time.monotonic() > exp:
            self._d.pop(key, None)
            return None
        return data

    def set(self, key: str, data: Any, ttl: float) -> None:
        self._d[key] = (time.monotonic() + ttl, data)

    def clear(self) -> None:
        self._d.clear()


CACHE = TTLCache()
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0), headers={"User-Agent": USER_AGENT},
                                    follow_redirects=True)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


_SECRET_URL = re.compile(r"bot\d+:[\w-]+")


def _redact(msg: str) -> str:
    """Never let a Telegram bot token (it sits in the URL path) reach logs or the health table."""
    return _SECRET_URL.sub("bot***", msg)


async def request_json(name: str, method: str, url: str, *, params: dict[str, Any] | None = None,
                       json: Any = None, headers: dict[str, str] | None = None, retries: int = 2,
                       limiter: TokenBucket | None = None, weight: float = 1.0) -> Any:
    """HTTP with backoff on 429/5xx/network errors. Raises SourceError after the last attempt."""
    last: str = ""
    for attempt in range(retries + 1):
        if limiter is not None:
            await limiter.acquire(weight)
        t0 = time.perf_counter()
        try:
            r = await client().request(method, url, params=params, json=json, headers=headers)
            ms = int((time.perf_counter() - t0) * 1000)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                record(name, False, ms, last)
                retry_after = r.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else (2 ** attempt) + random.random()
                await asyncio.sleep(min(delay, 20))
                continue
            if r.status_code >= 400:
                last = _redact(f"HTTP {r.status_code}: {r.text[:200]}")
                record(name, False, ms, last)
                raise SourceError(f"{name}: {last}")
            record(name, True, ms)
            return r.json()
        except (httpx.TransportError, httpx.TimeoutException) as e:
            ms = int((time.perf_counter() - t0) * 1000)
            last = _redact(f"{type(e).__name__}: {e}")
            record(name, False, ms, last)
            if attempt < retries:
                await asyncio.sleep(min(0.5 * (2 ** attempt) + random.random() * 0.5, 5))
    raise SourceError(f"{name}: {last or 'failed'}")


async def guarded(name: str, fn: Callable[[], Awaitable[T]], cache_key: str | None = None,
                  ttl: float = 0) -> SourceResult[T]:
    """Run a fetch, convert any failure into an unavailable result, optionally TTL-cache successes."""
    if cache_key:
        hit = CACHE.get(cache_key)
        if hit is not None:
            data, at = hit
            return SourceResult(name, True, data, fetched_at=at, cached=True)
    t0 = time.perf_counter()
    try:
        data = await fn()
    except Exception as e:
        return SourceResult(name, False, error=str(e)[:500], latency_ms=int((time.perf_counter() - t0) * 1000))
    res = SourceResult(name, True, data, latency_ms=int((time.perf_counter() - t0) * 1000))
    if cache_key and ttl > 0:
        CACHE.set(cache_key, (data, res.fetched_at), ttl)
    return res
