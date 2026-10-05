"""Validator: was the TEST sound? (Not: did the system pass.)

Deterministic part only. Ambiguous cases are surfaced as flags for a later LLM pass.
"""

from enum import Enum

from pydantic import BaseModel

from swarm.results import RunMetrics
from swarm.spec import Spec

MISSING_METRICS_TOLERANCE = 0.20
CLIENT_ERROR_BURST = 0.10  # share of unexpected 4xx that means "measured nothing"
CLIENT_ERROR_STATUSES = frozenset({401, 403, 404, 409, 422})


class Flaw(str, Enum):
    TECHNICAL = "technical"  # rerun the same spec after a reset, no LLM
    DESIGN = "design"  # back to the originating tester with the reason


class Validity(BaseModel):
    valid: bool
    flaw: Flaw | None = None
    reasons: list[str] = []
    flags: list[str] = []  # valid, but worth a look (e.g. signals never appeared)


def missing_signals(spec: Spec, m: RunMetrics) -> list[str]:
    return [
        s.metric
        for s in spec.mechanism_signals
        if s.metric not in m.signals or not s.holds(m.signals[s.metric])
    ]


def _technical(m: RunMetrics, spec: Spec, cpu_limit: float) -> list[str]:
    reasons: list[str] = []
    if m.tool_crashed:
        reasons.append("load tool crashed")
    if m.requests == 0:
        reasons.append("no requests were recorded")
    if m.metrics_missing_frac > MISSING_METRICS_TOLERANCE:
        reasons.append(f"{m.metrics_missing_frac:.0%} of metric samples missing")
    if m.fault_confirmed is False:
        reasons.append("fault was not confirmed applied")
    h = m.host
    if h.loadgen_cpu_peak_pct > cpu_limit:
        reasons.append(f"load generator CPU saturated ({h.loadgen_cpu_peak_pct:.0f}%)")
    if h.cpu_peak_pct > cpu_limit:
        reasons.append(f"host CPU saturated ({h.cpu_peak_pct:.0f}%)")
    if h.swapping:
        reasons.append("host was swapping")
    if m.duration_s < spec.expected_stimulus.min_duration_s and not m.aborted:
        reasons.append(f"run ended early ({m.duration_s:.0f}s) without an abort")
    return reasons


def _unexpected_client_errors(spec: Spec, m: RunMetrics) -> float:
    if m.requests == 0:
        return 0.0
    expected = set(spec.expected_stimulus.expected_status)
    bad = sum(n for s, n in m.status_counts.items() if s in CLIENT_ERROR_STATUSES and s not in expected)
    return bad / m.requests


def validate_run(spec: Spec, m: RunMetrics, host_cpu_limit_pct: float = 85.0) -> Validity:
    tech = _technical(m, spec, host_cpu_limit_pct)
    if tech:  # measurements can't be trusted, so don't also second-guess the design
        return Validity(valid=False, flaw=Flaw.TECHNICAL, reasons=tech)

    share = _unexpected_client_errors(spec, m)
    if share > CLIENT_ERROR_BURST:
        return Validity(
            valid=False,
            flaw=Flaw.DESIGN,
            reasons=[f"{share:.0%} of requests were unexpected 4xx: bad credentials, "
                     "payload or leftover state, so the test measured nothing"],
        )

    flags: list[str] = []
    if m.achieved_vus < spec.expected_stimulus.min_achieved_vus:
        # Generator is healthy (checked above), so the target is the limit: valid, but notable.
        flags.append(f"low_achieved_vus:{m.achieved_vus:.0f}<{spec.expected_stimulus.min_achieved_vus}")
    flags.extend(f"mechanism_signal_missing:{name}" for name in missing_signals(spec, m))
    return Validity(valid=True, flags=flags)
