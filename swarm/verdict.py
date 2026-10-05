"""Verdict: did the SYSTEM pass? Plain arithmetic on a valid run, relative to baseline."""

from enum import Enum

from pydantic import BaseModel

from swarm.manifest import SLO
from swarm.results import Baseline, RunMetrics
from swarm.spec import Spec
from swarm.validator import missing_signals

BORDERLINE = 0.20  # within 20% of a threshold counts as borderline


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class Confirm(str, Enum):
    NONE = "none"
    REPEAT = "repeat"  # clear fail: run once more, both must fail
    MEDIAN3 = "median3"  # borderline: run three times, judge the median


class Verdict(BaseModel):
    outcome: Outcome
    confirm: Confirm = Confirm.NONE
    weak_pass: bool = False  # clean numbers, but the stimulus never provably reached the limit
    reasons: list[str] = []


def _near(value: float, threshold: float) -> bool:
    return threshold > 0 and abs(value - threshold) <= BORDERLINE * threshold


def judge(spec: Spec, baseline: Baseline, m: RunMetrics, slo: SLO) -> Verdict:
    """Call only on a run the validator accepted."""
    reasons: list[str] = []
    failed = False
    borderline = False
    noise_blocked = False

    p95 = m.p95_ms
    rel_limit = baseline.p95_ms_median * slo.p95_vs_baseline_max
    if p95 > rel_limit:
        if p95 > baseline.p95_band_high:
            failed = True
            reasons.append(f"p95 {p95:.0f}ms > {slo.p95_vs_baseline_max}x baseline "
                           f"({rel_limit:.0f}ms) and outside noise band (<= {baseline.p95_band_high:.0f}ms)")
        else:
            noise_blocked = True
            reasons.append(f"p95 {p95:.0f}ms exceeds the ratio but is inside the baseline noise band")
    borderline |= _near(p95, rel_limit)

    if slo.p95_ms_ceiling is not None:
        if p95 > slo.p95_ms_ceiling:
            failed = True
            reasons.append(f"p95 {p95:.0f}ms > absolute ceiling {slo.p95_ms_ceiling:.0f}ms")
        borderline |= _near(p95, slo.p95_ms_ceiling)

    err_limit = slo.error_rate_max + baseline.error_rate_median
    if m.error_rate > err_limit:
        failed = True
        reasons.append(f"error rate {m.error_rate:.1%} > limit {err_limit:.1%}")
    borderline |= _near(m.error_rate, err_limit)

    if failed:
        return Verdict(outcome=Outcome.FAIL,
                       confirm=Confirm.MEDIAN3 if borderline else Confirm.REPEAT, reasons=reasons)
    if noise_blocked:
        return Verdict(outcome=Outcome.INCONCLUSIVE, reasons=reasons)

    absent = missing_signals(spec, m)
    if absent:
        return Verdict(outcome=Outcome.INCONCLUSIVE, weak_pass=True,
                       reasons=[f"limits met, but mechanism signals never appeared: {absent}"])
    return Verdict(outcome=Outcome.PASS,
                   confirm=Confirm.MEDIAN3 if borderline else Confirm.NONE,
                   reasons=["all limits met and mechanism signals observed"])


def resolve_repeat(first: Verdict, second: Verdict) -> Verdict:
    """Combine a clear fail with its single confirmation run."""
    if first.outcome is Outcome.FAIL and second.outcome is Outcome.FAIL:
        return Verdict(outcome=Outcome.FAIL, reasons=[*first.reasons, "confirmed on repeat"])
    return Verdict(outcome=Outcome.INCONCLUSIVE,
                   reasons=["unstable: the repeat run disagreed with the first", *first.reasons])


def median_run(runs: list[RunMetrics]) -> RunMetrics:
    """For borderline results: judge the run whose p95 is the median of the set."""
    if not runs:
        raise ValueError("no runs")
    return sorted(runs, key=lambda r: r.p95_ms)[len(runs) // 2]
