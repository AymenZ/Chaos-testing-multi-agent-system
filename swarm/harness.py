# swarm/harness.py
"""The minimum routing needed to try the nodes together before the graph exists. NOT the graph.

For one experiment: run it, rerun technical flaws (up to the manifest's attempt limit), confirm a clear
fail with one repeat, judge borderline results on the median of three. A design flaw is recorded as
"could not test": in the full graph it goes back to the tester. Your graph replaces all of this.
"""
from typing import Callable

from swarm.baseline import BaselineSummary
from swarm.guardrail import Experiment
from swarm.manifest import Manifest
from swarm.measurement import Measurement
from swarm.report import ExperimentResult, judged_result, untestable_result
from swarm.validator import Validity
from swarm.verdict import Confirm, Outcome, Verdict, judge, median_run, resolve_repeat

# (experiment, run name) -> (measurement if it was a fair test else None, reasons, validity if one was made)
RunOnce = Callable[[Experiment, str], tuple[Measurement | None, list[str], Validity | None]]


def run_one(m: Manifest, exp: Experiment, summary: BaselineSummary, run_once: RunOnce,
            say: Callable[[str], None] = lambda _: None) -> ExperimentResult:
    rules = m.rules.default
    reasons: list[str] = []
    for attempt in range(1, m.budgets.max_attempts + 1):
        say(f"{exp.id}: attempt {attempt}, {exp.endpoint} at {exp.load_multiple:g}x ({exp.target_rps:.0f} rps), {exp.total_s}s")
        meas, reasons, validity = run_once(exp, f"{exp.id}-try{attempt}")
        if meas is not None:
            break
        if validity is not None and validity.flaw == "design":
            return untestable_result(exp, [*reasons, "(in the full graph this goes back to the tester)"], attempt)
        say(f"{exp.id}: not a fair test ({'; '.join(reasons)}); rerunning")
    else:
        return untestable_result(exp, reasons, m.budgets.max_attempts)

    verdict = judge(exp, meas, summary, rules, validity.flags)
    say(f"{exp.id}: {verdict.outcome.value}" + (f", confirming ({verdict.confirm.value})" if verdict.confirm is not Confirm.NONE else ""))
    if verdict.confirm is Confirm.REPEAT:
        again, why, _ = run_once(exp, f"{exp.id}-confirm")
        if again is not None:
            verdict = resolve_repeat(verdict, judge(exp, again, summary, rules, validity.flags))
        else:
            verdict = Verdict(outcome=Outcome.INCONCLUSIVE,
                              reasons=["the confirmation run was not a fair test", *why, *verdict.reasons])
    elif verdict.confirm is Confirm.MEDIAN3:
        extra = [run_once(exp, f"{exp.id}-median{i}")[0] for i in (2, 3)]
        meas = median_run([meas] + [r for r in extra if r is not None])
        verdict = judge(exp, meas, summary, rules, validity.flags)
    say(f"{exp.id}: final {verdict.outcome.value}")
    return judged_result(exp, meas, verdict, validity.flags, attempt)
