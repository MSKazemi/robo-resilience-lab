"""Small, deterministic statistics helpers (stdlib only).

Policy (enforced here, not only documented):

* Fewer than ``MIN_RUNS`` (3) independent runs => NO interval and NO
  significance statement is produced; callers print ``NO_CLAIM``.
* Intervals are seeded percentile bootstraps of the mean over RUN-LEVEL values
  (the run is the unit of independence), so output is byte-reproducible.
* No p-values and no "significant" wording are ever produced.
* n >= 3 is a *minimum to print an interval*, not a power analysis: bootstrap
  intervals from a handful of runs are unstable and descriptive only.
"""

from __future__ import annotations

import hashlib
import math
import random
from typing import Optional, Sequence

MIN_RUNS = 3
NO_CLAIM = "n<3: no statistical claim"
BOOTSTRAP_B = 2000
BOOTSTRAP_BASE_SEED = 20261113
LEVEL = 0.95


def derived_seed(*parts: str) -> int:
    """Stable 32-bit seed from labels (independent of iteration order)."""
    h = hashlib.sha256((str(BOOTSTRAP_BASE_SEED) + ":" + ":".join(parts)).encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _boot_means(xs: Sequence[float], rng: random.Random, b: int) -> list:
    n = len(xs)
    out = []
    for _ in range(b):
        acc = 0.0
        for _ in range(n):
            acc += xs[int(rng.random() * n)]
        out.append(acc / n)
    return out


def _percentiles(vals: list) -> tuple:
    vals.sort()
    b = len(vals)
    alpha = (1.0 - LEVEL) / 2.0
    return vals[int(math.floor(alpha * (b - 1)))], vals[int(math.ceil((1.0 - alpha) * (b - 1)))]


def ci_mean(xs: Sequence[float], seed: int, b: int = BOOTSTRAP_B) -> Optional[tuple]:
    """95% percentile-bootstrap interval of the mean, or None if n < MIN_RUNS."""
    if len(xs) < MIN_RUNS:
        return None
    rng = random.Random(seed)
    return _percentiles(_boot_means(list(xs), rng, b))


def ci_diff_mean(a: Sequence[float], b_vals: Sequence[float], seed: int, b: int = BOOTSTRAP_B) -> Optional[tuple]:
    """95% percentile-bootstrap interval of mean(b_vals) - mean(a); None if either n < MIN_RUNS."""
    if len(a) < MIN_RUNS or len(b_vals) < MIN_RUNS:
        return None
    rng = random.Random(seed)
    ma = _boot_means(list(a), rng, b)
    mb = _boot_means(list(b_vals), rng, b)
    return _percentiles([y - x for x, y in zip(ma, mb)])


def fmt(x: float) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s
