# swarm/verdict.py
"""Verdict: did the BACKEND pass? Plain arithmetic on a run the Validator accepted. No LLM.

Judged against the manifest's rules, relative to the baseline and its normal wobble:
- aborted early, or error rate above the limit: fail, at any load.
- p95 more than ratio x the baseline AND outside everything the baseline ever showed: fail, but only for
  loads up to measured capacity. Capacity is where that rule starts to break, so above it a p95 "fail"
  would be true by definition; above capacity the error rate and early abort are what count.
- within the baseline's wobble: inconclusive, never a fail.
- A pass only counts if the card's mechanism signals were observed. Clean numbers without them mean the
  load may never have reached the limit: inconclusive (weak pass), not pass.
"""
from enum import Enum

from pydantic import BaseModel

from swarm.baseline import BaselineSummary, clearly_slower
from swarm.guardrail import Experiment
from swarm.manifest import RuleSet
from swarm.measurement import Measurement

BORDERLINE = 0.20             # within 20% of a threshold counts as borderline
# "Observed" thresholds for each catalog signal. Starting guesses, to be tuned with real runs.
LATENCY_RISE = 1.5            # p95 at least this many times the baseline p95
ERROR_RISE = 0.001            # error rate at least this far above the baseline's
KEEP_UP = 0.95                # achieved rate below this fraction of the target means it could not keep up
STRESS_CPU_PCT = 70.0         # API or database CPU peak
DB_CONNECTIONS_STRESS = 10.0  # connections in use; the common default pool is 5 + 10


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class Confirm(str, Enum):
    NONE = "none"
    REPEAT = "repeat"      # clear fail: run once more, both must fail
    MEDIAN3 = "median3"    # borderline: run three times and judge the median


class Verdict(BaseModel):
    outcome: Outcome
    confirm: Confirm = Confirm.NONE
    weak_pass: bool = False                # clean numbers, but the stimulus never provably reached the limit
    reasons: list[str] = []
    observed: list[str] = []
    missing: list[str] = []


def observed_signals(exp: Experiment, meas: Measurement, base: BaselineSummary) -> tuple[list[str], list[str]]:
    """Which of the card's expected signals actually appeared. Returns (observed, missing)."""
    sig = meas.signals
    seen = {
        "request_latency_p95": meas.p95_ms >= LATENCY_RISE * base.p95_median_ms,
        "error_rate": meas.error_rate >= base.error_rate_median + ERROR_RISE,
        "achieved_request_rate": meas.achieved_rps < KEEP_UP * exp.target_rps,
        "api_cpu": sig.get("api_cpu", 0.0) >= STRESS_CPU_PCT,
        "db_cpu": sig.get("db_cpu", 0.0) >= STRESS_CPU_PCT,
        "db_connections": sig.get("db_connections", 0.0) >= DB_CONNECTIONS_STRESS,
    }
    names = [s.value for s in exp.signals]
    return [n for n in names if seen.get(n)], [n for n in names if not seen.get(n)]


def _near(value: float, threshold: float) -> bool:
    return threshold > 0 and abs(value - threshold) <= BORDERLINE * threshold


def judge(exp: Experiment, meas: Measurement, base: BaselineSummary, rules: RuleSet,
          flags: list[str] | None = None) -> Verdict:
    """Call only on a run the Validator accepted; pass its flags through."""
    flags = flags or []
    reasons: list[str] = []
    failed = borderline = noise_blocked = False

    if meas.aborted:
        failed = True
        reasons.append(meas.abort_reason or "run was aborted for too many errors")
    err_limit = rules.error_rate_max + base.error_rate_median
    if meas.error_rate > err_limit:
        failed = True
        reasons.append(f"error rate {meas.error_rate:.1%} is above the limit {err_limit:.1%}")
    borderline |= _near(meas.error_rate, err_limit)

    if exp.load_multiple <= 1.0:
        rel_limit = rules.p95_vs_baseline_max * base.p95_median_ms
        if clearly_slower(meas.p95_ms, base, rules.p95_vs_baseline_max):
            failed = True
            reasons.append(f"p95 {meas.p95_ms:.0f} ms is more than {rules.p95_vs_baseline_max}x the baseline "
                           f"({base.p95_median_ms:.0f} ms) and outside its usual range (up to {base.p95_max_ms:.0f} ms)")
        elif meas.p95_ms > rel_limit:
            noise_blocked = True
            reasons.append(f"p95 {meas.p95_ms:.0f} ms is over the ratio but inside the baseline's usual range")
        borderline |= _near(meas.p95_ms, rel_limit)
    else:
        reasons.append(f"p95 ({meas.p95_ms:.0f} ms) is not judged above measured capacity "
                       f"(this load was {exp.load_multiple}x); error rate and early abort are")

    if failed:
        return Verdict(outcome=Outcome.FAIL, confirm=Confirm.MEDIAN3 if borderline else Confirm.REPEAT, reasons=reasons)
    if noise_blocked:
        return Verdict(outcome=Outcome.INCONCLUSIVE, reasons=reasons)
    if any(f.startswith("under_delivered") for f in flags):
        return Verdict(outcome=Outcome.INCONCLUSIVE, reasons=[*reasons, "the designed load was not delivered, "
                       "so a clean result says little about it"])

    observed, missing = observed_signals(exp, meas, base)
    if missing:
        return Verdict(outcome=Outcome.INCONCLUSIVE, weak_pass=True, observed=observed, missing=missing,
                       reasons=[*reasons, f"limits were met, but the expected signals never appeared: {missing}"])
    return Verdict(outcome=Outcome.PASS, confirm=Confirm.MEDIAN3 if borderline else Confirm.NONE,
                   observed=observed, reasons=[*reasons, "limits met and the expected signals were observed"])


def resolve_repeat(first: Verdict, second: Verdict) -> Verdict:
    """Combine a clear fail with its single confirmation run."""
    if first.outcome is Outcome.FAIL and second.outcome is Outcome.FAIL:
        return Verdict(outcome=Outcome.FAIL, reasons=[*first.reasons, "confirmed on repeat"])
    return Verdict(outcome=Outcome.INCONCLUSIVE,
                   reasons=["unstable: the repeat run disagreed with the first", *first.reasons])


def median_run(runs: list[Measurement]) -> Measurement:
    """For borderline results: judge the run whose p95 is the median of the set."""
    if not runs:
        raise ValueError("no runs")
    return sorted(runs, key=lambda r: r.p95_ms)[len(runs) // 2]
