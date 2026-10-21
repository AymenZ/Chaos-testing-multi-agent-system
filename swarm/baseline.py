from dataclasses import dataclass
from statistics import median
from typing import Literal

from swarm.manifest import Limits, RuleSet
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


# --- is the capacity number usable? -------------------------------------------------------------

MIN_USEFUL_MULTIPLE = 1.2   # starting guess: below this the limits leave no room to stress past capacity
AT_LIMIT = 0.95             # a ramp that reached 95% of max_rps has hit the manifest's ceiling


@dataclass(frozen=True)
class CapacityContext:
    """All the request tester gets to know about capacity. It never sees raw ramp results."""
    rps: float
    kind: Literal["exact", "lower_bound"]
    max_multiple: float        # the most load, as a multiple of rps, the manifest limits allow


@dataclass(frozen=True)
class CapacityCheck:
    """The graph routes on `outcome`. ok / lower_bound: continue. retry: rerun the baseline or ramp.
    unfit: stop, could not test. cannot_sustain: stop, and report it as a finding."""
    outcome: Literal["ok", "lower_bound", "retry", "unfit", "cannot_sustain"]
    reason: str
    context: CapacityContext | None = None
    noisy: bool = False        # flagged, not blocking: results inside the noise band stay inconclusive


def ramp_rates(normal_rps: float, factor: float, max_rps: float) -> list[float]:
    """Request rates for the capacity ramp: usual traffic, then ever larger steps, ending at the limit."""
    rates, r = [], normal_rps
    while r < max_rps:
        rates.append(round(r, 2))
        r *= factor
    if rates and max_rps - rates[-1] < (1 - AT_LIMIT) * max_rps:     # a step this close to the limit is the limit
        rates.pop()
    return [*rates, max_rps]


def check_capacity(base: BaselineSummary, cap: Capacity, limits: Limits, normal_rps: float,
                   retried: bool = False) -> CapacityCheck:
    """Deterministic gate between Baseline and the tester. `retried` says the baseline or ramp already
    ran twice. The ramp starts at normal_rps (usual traffic), so an unhealthy first step is a finding."""
    def again(reason: str) -> CapacityCheck:
        return CapacityCheck("unfit" if retried else "retry", reason, noisy=base.noisy)

    if base.low_samples:
        return again("baseline runs had too few requests for a reliable p95")
    if cap.status == "below_start" or not cap.rps:
        return CapacityCheck("cannot_sustain", f"even usual traffic ({normal_rps:g} rps) was unhealthy: "
                             "the backend cannot sustain its normal load in this sandbox", noisy=base.noisy)

    kind: Literal["exact", "lower_bound"] = "exact"
    reason = f"capacity found at {cap.rps:.0f} rps"
    if cap.status == "not_reached":
        if cap.rps < AT_LIMIT * limits.max_rps and not retried:
            return again("every step was healthy but the ramp ended below max_rps: the ramp was too short")
        kind = "lower_bound"
        reason = f"every ramp step was healthy: capacity is at least {cap.rps:.0f} rps"

    max_multiple = limits.max_rps / cap.rps
    if max_multiple < MIN_USEFUL_MULTIPLE:
        return CapacityCheck("unfit", f"capacity ({cap.rps:.0f} rps) leaves no headroom under max_rps "
                             f"({limits.max_rps}): raise the limit to test past it", noisy=base.noisy)
    return CapacityCheck("ok" if kind == "exact" else "lower_bound", reason,
                         CapacityContext(cap.rps, kind, round(max_multiple, 2)), base.noisy)
