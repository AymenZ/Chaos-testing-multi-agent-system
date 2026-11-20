# swarm/validator.py
"""Validator: was the TEST fair? (Not: did the backend pass; that is the Verdict's job.) Plain code.

A broken test must never be reported as a backend failure, and a test that measured nothing must
never be reported as a pass. Two kinds of flaw, handled differently by the graph:
- technical: the setup failed (our own machine, missing numbers). Rerun the same card, no LLM.
- design: the card itself was flawed (e.g. wrong credentials, so every request was rejected).
  Send it back to the tester with the reason.
"""
from dataclasses import dataclass, field
from typing import Literal

from swarm.guardrail import Experiment
from swarm.measurement import Measurement

CLIENT_ERRORS = frozenset({400, 401, 403, 404, 409, 422})
CLIENT_ERROR_BURST = 0.10      # starting guess: above this share, requests were being refused, not served


@dataclass(frozen=True)
class Validity:
    valid: bool
    flaw: Literal["technical", "design"] | None = None
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)     # valid, but the Verdict should know


def client_error_share(meas: Measurement) -> float:
    if meas.requests == 0:
        return 0.0
    return sum(n for s, n in meas.status_counts.items() if s in CLIENT_ERRORS) / meas.requests


def validate(exp: Experiment, meas: Measurement, host_cpu_limit_pct: float) -> Validity:
    technical: list[str] = []
    if meas.requests == 0:
        technical.append("no requests were recorded")
    if meas.loadgen_cpu_max_pct is None or meas.host_cpu_max_pct is None:
        technical.append("machine metrics are missing, so a saturated load generator cannot be ruled out")
    else:
        if meas.loadgen_cpu_max_pct > host_cpu_limit_pct:
            technical.append(f"the load generator was saturated ({meas.loadgen_cpu_max_pct:.0f}% CPU): "
                             "we measured our own tool")
        if meas.host_cpu_max_pct > host_cpu_limit_pct:
            technical.append(f"the whole machine was saturated ({meas.host_cpu_max_pct:.0f}% CPU)")
    if technical:                       # numbers can't be trusted, so don't second-guess the design as well
        return Validity(False, "technical", technical)

    share = client_error_share(meas)
    if share > CLIENT_ERROR_BURST:
        return Validity(False, "design", [
            f"{share:.0%} of requests were refused with client errors "
            f"({sorted(s for s in meas.status_counts if s in CLIENT_ERRORS)}): bad credentials, payload or "
            "leftover state, so the test measured nothing about load"])

    flags: list[str] = []
    if meas.aborted:
        flags.append("aborted")
    else:
        delivered = meas.achieved_rps / exp.target_rps
        if delivered < exp.min_delivered:
            if meas.dropped_iterations == 0:
                return Validity(False, "technical", [
                    f"only {delivered:.0%} of the target load was delivered and nothing explains it "
                    "(no dropped iterations, load generator healthy)"])
            flags.append(f"under_delivered:{delivered:.0%}")     # the backend was too slow to be fed more
    return Validity(True, flags=flags)
