"""Original Phase 2 execution interface sketch (unused). Live orders are placed by `perpdesk.live`; nothing in this
package can place an order."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Mode(StrEnum):
    SIGNALS = "signals"  # Phase 1: no orders
    PAPER = "paper"  # simulated fills against the live book
    LIVE = "live"  # real orders via a Hyperliquid API (agent) wallet, after typed confirmation


@dataclass(frozen=True)
class OrderIntent:
    """Everything needed to place a plan as one atomic group: limit entry + exchange-side SL/TP (reduce-only)."""

    plan_id: int
    coin: str
    is_buy: bool
    qty: float
    entry_px: float
    sl_px: float
    tp1_px: float
    tp2_px: float
    tp1_fraction: float
    leverage: int
    isolated: bool = True
    entry_type: str = "limit"
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def client_order_id(self) -> str:
        """Deterministic 128-bit id (Hyperliquid cloid format) so retries are idempotent."""
        h = hashlib.sha256(f"perpdesk:{self.plan_id}:{self.coin}:{self.is_buy}:{self.qty}".encode()).hexdigest()
        return "0x" + h[:32]


@dataclass
class ExecutionResult:
    accepted: bool
    message: str
    exchange_ids: list[str] = field(default_factory=list)


class Executor(Protocol):
    mode: Mode

    async def place(self, intent: OrderIntent) -> ExecutionResult: ...

    async def cancel_all(self, coin: str | None = None) -> ExecutionResult: ...

    async def move_stop_to_breakeven(self, plan_id: int) -> ExecutionResult: ...

    async def reconcile(self) -> list[str]: ...

    async def halt(self, reason: str) -> None: ...


class NoopExecutor:
    """Phase 1 executor: records nothing, places nothing."""

    mode = Mode.SIGNALS

    async def place(self, intent: OrderIntent) -> ExecutionResult:
        return ExecutionResult(False, "Execution is disabled in Phase 1 (signals only).")

    async def cancel_all(self, coin: str | None = None) -> ExecutionResult:
        return ExecutionResult(False, "Execution is disabled in Phase 1.")

    async def move_stop_to_breakeven(self, plan_id: int) -> ExecutionResult:
        return ExecutionResult(False, "Execution is disabled in Phase 1.")

    async def reconcile(self) -> list[str]:
        return []

    async def halt(self, reason: str) -> None:
        return None


def get_executor() -> Executor:
    return NoopExecutor()
