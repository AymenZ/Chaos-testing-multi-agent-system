from dataclasses import dataclass
from statistics import median
from typing import Literal

from swarm.manifest import RuleSet
from swarm.measurement import Measurement

NOISY_SPREAD = 0.30     # starting guess: repeats differing by over 30% means too noisy
MIN_SAMPLES = 200       # fewer requests than this makes a p95 unreliable
MIN_ACHIEVED = 0.95     # a healthy step must reach 95% of its target rate


@dataclass(frozen=True)
class BaselineSummary:
    rps: float
    p95_median_ms: float
    p95_min_ms: float
    p95_max_ms: float
    error_rate_median: float
    spread: float          # (max - min) / median
    noisy: bool
    low_samples: bool


def summarize_baseline(runs: list[Measurement]) -> BaselineSummary:
    if len(runs) < 3:
        raise ValueError("need at least 3 baseline runs")
    p95s = [m.p95_ms for m in runs]
    med = median(p95s)
    spread = (max(p95s) - min(p95s)) / med if med > 0 else float("inf")
    return BaselineSummary(
        rps=median(m.achieved_rps for m in runs),
        p95_median_ms=med,
        p95_min_ms=min(p95s),
        p95_max_ms=max(p95s),
        error_rate_median=median(m.error_rate for m in runs),
        spread=spread,
        noisy=spread > NOISY_SPREAD,
        low_samples=any(m.requests < MIN_SAMPLES for m in runs),
    )


def clearly_slower(p95_ms: float, base: BaselineSummary, ratio: float) -> bool:
    """Worse than 'ratio' times normal AND outside everything the baseline ever showed."""
    return p95_ms > ratio * base.p95_median_ms and p95_ms > base.p95_max_ms


def step_healthy(step: Measurement, base: BaselineSummary, rules: RuleSet) -> bool:
    return (
        not clearly_slower(step.p95_ms, base, rules.p95_vs_baseline_max)
        and step.error_rate <= rules.error_rate_max
        and step.achieved_rps >= MIN_ACHIEVED * step.target_rps
    )


@dataclass(frozen=True)
class Capacity:
    status: Literal["found", "below_start", "not_reached"]
    rps: float | None            # highest healthy step
    first_bad_rps: float | None  # first unhealthy step


def find_knee(steps: list[Measurement], base: BaselineSummary, rules: RuleSet) -> Capacity:
    if not steps:
        raise ValueError("need at least one ramp step")
    last_good = None
    for s in sorted(steps, key=lambda s: s.target_rps):
        if step_healthy(s, base, rules):
            last_good = s.target_rps
        elif last_good is None:
            return Capacity("below_start", None, s.target_rps)    # ramp started too high
        else:
            return Capacity("found", last_good, s.target_rps)
    return Capacity("not_reached", last_good, None)               # ramp ended too low