"""Map confidence -> P(TP1 before SL) using out-of-sample outcomes.

Isotonic regression (pool-adjacent-violators) when there is enough data, Platt scaling (1-D
logistic regression) for small samples. Both are hand-written to keep the serverless bundle small.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from perpdesk.tracking.stats import wilson


@dataclass
class Calibrator:
    kind: str  # isotonic|platt|constant
    xs: list[float]  # isotonic block upper bounds, or [a, b] for platt
    ps: list[float]
    ns: list[int]
    n: int

    def predict(self, conf: float) -> tuple[float, float, float, int]:
        """(p, lo, hi, n_used) for a confidence score."""
        if self.kind == "constant" or not self.xs:
            p = self.ps[0] if self.ps else 0.5
            lo, hi = wilson(round(p * self.n), self.n)
            return p, lo, hi, self.n
        if self.kind == "platt":
            a, b = self.xs
            p = 1 / (1 + math.exp(-(a * conf + b)))
            lo, hi = wilson(round(p * self.n), self.n)
            return p, lo, hi, self.n
        i = int(np.searchsorted(np.asarray(self.xs), conf, side="left"))
        i = min(i, len(self.ps) - 1)
        p, nb = self.ps[i], self.ns[i]
        lo, hi = wilson(round(p * nb), nb)
        return p, lo, hi, nb

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "xs": self.xs, "ps": self.ps, "ns": self.ns, "n": self.n}

    @staticmethod
    def from_json(d: dict[str, Any]) -> Calibrator:
        return Calibrator(d["kind"], list(d["xs"]), list(d["ps"]), [int(x) for x in d["ns"]], int(d["n"]))


def isotonic(conf: Sequence[float], won: Sequence[bool], min_block: int = 10) -> Calibrator:
    x = np.asarray(conf, dtype=float)
    y = np.asarray(won, dtype=float)
    order = np.argsort(x, kind="stable")
    x, y = x[order], y[order]
    # blocks: [sum, count, max_x]
    blocks: list[list[float]] = []
    for xi, yi in zip(x, y, strict=True):
        blocks.append([yi, 1.0, xi])
        while len(blocks) > 1 and (blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]
                                   or blocks[-2][1] < min_block):
            s, cnt, mx = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += cnt
            blocks[-1][2] = mx
    # merge a too-small final block backwards
    while len(blocks) > 1 and blocks[-1][1] < min_block:
        s, cnt, mx = blocks.pop()
        blocks[-1][0] += s
        blocks[-1][1] += cnt
        blocks[-1][2] = mx
    xs = [b[2] for b in blocks]
    ps = [b[0] / b[1] for b in blocks]
    ns = [int(b[1]) for b in blocks]
    return Calibrator("isotonic", xs, ps, ns, len(x))


def platt(conf: Sequence[float], won: Sequence[bool], iters: int = 50) -> Calibrator:
    x = np.asarray(conf, dtype=float) / 100.0
    y = np.asarray(won, dtype=float)
    a, b = 0.0, math.log((y.mean() + 1e-6) / (1 - y.mean() + 1e-6))
    for _ in range(iters):
        z = a * x + b
        p = 1 / (1 + np.exp(-z))
        w = p * (1 - p) + 1e-9
        g_a, g_b = float(((p - y) * x).sum()), float((p - y).sum())
        h_aa, h_ab, h_bb = float((w * x * x).sum()) + 1e-6, float((w * x).sum()), float(w.sum()) + 1e-6
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        a -= (h_bb * g_a - h_ab * g_b) / det
        b -= (-h_ab * g_a + h_aa * g_b) / det
        a = max(-50.0, min(50.0, a))
    # store slope per confidence point (x was scaled /100)
    return Calibrator("platt", [a / 100.0, b], [], [], len(x))


def fit(conf: Sequence[float], won: Sequence[bool]) -> Calibrator:
    n = len(conf)
    if n == 0:
        return Calibrator("constant", [], [0.5], [0], 0)
    if n < 30:
        return Calibrator("constant", [], [float(np.mean(won))], [n], n)
    if n < 200:
        return platt(conf, won)
    return isotonic(conf, won)


def reliability(pred: Sequence[float], won: Sequence[bool], bins: int = 10) -> tuple[list[dict[str, float]], float]:
    """Buckets of predicted probability vs actual win rate, and the weighted mean absolute gap."""
    p = np.asarray(pred, dtype=float)
    y = np.asarray(won, dtype=float)
    out: list[dict[str, float]] = []
    if p.size == 0:
        return out, 0.0
    edges = np.linspace(0, 1, bins + 1)
    err = 0.0
    for i in range(bins):
        m = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if not m.any():
            continue
        pred_m, act_m, cnt = float(p[m].mean()), float(y[m].mean()), int(m.sum())
        out.append({"lo": float(edges[i]), "hi": float(edges[i + 1]), "predicted": pred_m, "actual": act_m, "n": cnt})
        err += abs(pred_m - act_m) * cnt
    return out, err / p.size
