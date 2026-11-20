# swarm/report.py
"""Report: what was tried, what happened, and what was NOT covered. Plain code; it decides nothing.

It takes final results (the graph has already run retries and judged each experiment) and turns them
into one overall label plus a readable write-up. "Clean" only ever means "what we tried held up".

Labels: findings (a backend failure was confirmed, or it cannot hold usual traffic),
        incomplete (nothing failed, but something was inconclusive, untestable or skipped),
        clean (everything tried passed with its signals observed),
        not_run (the environment or capacity measurement stopped the run before any verdict).
"""
from pathlib import Path
from typing import Any, Literal

from swarm.baseline import BaselineSummary, CapacityCheck
from swarm.brief import Brief
from swarm.guardrail import Experiment, Rejection
from swarm.manifest import Strict
from swarm.measurement import Measurement
from swarm.preflight import PreflightResult
from swarm.tester import runnable_endpoints
from swarm.verdict import Verdict


class ExperimentResult(Strict):
    experiment: Experiment
    outcome: Literal["pass", "fail", "inconclusive", "could_not_test"]
    reasons: list[str]
    flags: list[str] = []
    weak_pass: bool = False
    measurement: Measurement | None = None
    attempts: int = 1


def judged_result(exp: Experiment, meas: Measurement, verdict: Verdict, flags: list[str] | None = None,
                  attempts: int = 1) -> ExperimentResult:
    return ExperimentResult(experiment=exp, outcome=verdict.outcome.value, reasons=verdict.reasons,
                            flags=flags or [], weak_pass=verdict.weak_pass, measurement=meas, attempts=attempts)


def untestable_result(exp: Experiment, reasons: list[str], attempts: int) -> ExperimentResult:
    """Retries ran out or the run could not be carried out. Never counted as a pass."""
    return ExperimentResult(experiment=exp, outcome="could_not_test", reasons=reasons, attempts=attempts)


class Report(Strict):
    label: Literal["clean", "incomplete", "findings", "not_run"]
    headline: str
    environment: dict[str, Any]
    baseline: dict[str, Any] | None
    capacity: dict[str, Any] | None
    results: list[ExperimentResult]
    findings: list[str]
    not_covered: list[str]
    notes: list[str]


def _count(results: list[ExperimentResult], outcome: str) -> int:
    return sum(r.outcome == outcome for r in results)


def build_report(*, preflight: PreflightResult | None = None, brief: Brief | None = None,
                 baseline: BaselineSummary | None = None, capacity: CapacityCheck | None = None,
                 results: list[ExperimentResult] | None = None, rejected: list[Rejection] | None = None,
                 truncated: bool = False) -> Report:
    results, rejected = results or [], rejected or []
    findings = [f"{r.experiment.id}: {r.experiment.endpoint} at {r.experiment.load_multiple:g}x capacity "
                f"({r.experiment.target_rps:.0f} rps): " + "; ".join(r.reasons)
                for r in results if r.outcome == "fail"]

    # --- the overall label -------------------------------------------------------------
    if preflight is not None and not preflight.ok:
        label, headline = "not_run", f"Not run: the environment failed at '{preflight.failed_step}': {preflight.reason}"
    elif capacity is not None and capacity.outcome == "cannot_sustain":
        label, headline = "findings", f"Finding: {capacity.reason}"
        findings.insert(0, capacity.reason)
    elif capacity is not None and capacity.outcome in ("unfit", "retry"):
        label, headline = "not_run", f"Not run: capacity could not be measured: {capacity.reason}"
    elif not results:
        label, headline = "not_run", "No experiment was run"
    else:
        n = len(results)
        summary = (f"{n} experiment{'s' if n != 1 else ''}: {_count(results, 'pass')} passed, "
                   f"{_count(results, 'fail')} failed, {_count(results, 'inconclusive')} inconclusive, "
                   f"{_count(results, 'could_not_test')} could not be tested")
        if findings:
            label, headline = "findings", summary
        elif _count(results, "inconclusive") or _count(results, "could_not_test") or rejected or truncated:
            label, headline = "incomplete", summary
        else:
            label, headline = "clean", summary

    # --- what was NOT covered -------------------------------------------------------------
    not_covered: list[str] = []
    if brief is not None:
        tested = {r.experiment.endpoint for r in results}
        for note in brief.output.endpoints:
            if note.load_suitability in ("avoid", "needs_data"):
                not_covered.append(f"{note.key}: not tested ({note.load_suitability})")
        not_covered += [f"{key}: runnable but no experiment targeted it" for key in runnable_endpoints(brief) if key not in tested]
        not_covered += [f"unknown to the planner: {u}" for u in brief.output.unknowns]
        if brief.dropped:
            not_covered.append(f"{len(brief.dropped)} planner claim(s) were dropped because they could not be verified")
    not_covered += [f"card {x.card_id} was rejected: {x.reason}" for x in rejected]
    not_covered += [f"{r.experiment.id}: could not be tested ({'; '.join(r.reasons)})" for r in results if r.outcome == "could_not_test"]
    not_covered += [f"{r.experiment.id}: inconclusive ({r.reasons[-1]})" for r in results if r.outcome == "inconclusive" and r.reasons]

    notes = ["Clean means only that the experiments listed here held up; it is not a claim that the backend has no problems."]
    if truncated:
        notes.append("The run was cut short by a budget; experiments that had not started were skipped.")
    if baseline is not None and baseline.noisy:
        notes.append("The baseline was noisy on this machine, so results inside its usual range are reported as inconclusive.")
    if any(r.weak_pass for r in results):
        notes.append("A weak pass means the numbers were clean but the expected signals never appeared, "
                     "so the load may not have reached the backend's limit.")
    if any("aborted" in r.flags for r in results):
        notes.append("Some experiments were stopped early because errors passed their limit; they are measured up to that point.")

    return Report(
        label=label, headline=headline,
        environment=(preflight.details if preflight and preflight.ok else {}),
        baseline=None if baseline is None else {
            "normal_rps": baseline.rps, "p95_median_ms": baseline.p95_median_ms,
            "p95_range_ms": [baseline.p95_min_ms, baseline.p95_max_ms],
            "error_rate": baseline.error_rate_median, "noisy": baseline.noisy},
        capacity=None if capacity is None else (
            {"rps": capacity.context.rps, "kind": capacity.context.kind, "max_multiple": capacity.context.max_multiple}
            if capacity.context else {"outcome": capacity.outcome, "reason": capacity.reason}),
        results=results, findings=findings, not_covered=not_covered, notes=notes)


# --- rendering ---------------------------------------------------------------------------

def _row(r: ExperimentResult, base_p95: float | None) -> str:
    e, m = r.experiment, r.measurement
    outcome = r.outcome.replace("_", " ") + (" (weak)" if r.weak_pass else "")
    load = f"{e.load_multiple:g}x ({e.target_rps:.0f} rps)"
    if m is None:
        return f"| {e.id} | {e.endpoint} | {load} | {outcome} | - | - | - |"
    p95 = f"{m.p95_ms:.0f} ms" + (f" ({m.p95_ms / base_p95:.1f}x)" if base_p95 else "")
    return f"| {e.id} | {e.endpoint} | {load} | {outcome} | {p95} | {m.error_rate:.1%} | {m.achieved_rps / e.target_rps:.0%} |"


def render_markdown(report: Report) -> str:
    out = [f"# Resilience test report", "", f"**Result: {report.label.upper().replace('_', ' ')}**. {report.headline}", ""]
    if report.environment:
        out += ["## Environment", ", ".join(f"{k}: {v}" for k, v in report.environment.items()), ""]
    if report.baseline or report.capacity:
        out.append("## Baseline and capacity")
        if report.baseline:
            b = report.baseline
            out.append(f"- Usual traffic ({b['normal_rps']:g} rps): p95 {b['p95_median_ms']:.0f} ms "
                       f"(range {b['p95_range_ms'][0]:.0f}-{b['p95_range_ms'][1]:.0f} ms), error rate {b['error_rate']:.2%}")
        if report.capacity:
            c = report.capacity
            out.append(f"- Capacity: {'at least ' if c['kind'] == 'lower_bound' else ''}{c['rps']:.0f} rps; the limits allow up to {c['max_multiple']}x"
                       if "rps" in c else f"- Capacity: {c['reason']}")
        out.append("")
    if report.results:
        base_p95 = report.baseline["p95_median_ms"] if report.baseline else None
        out += ["## Experiments", "", "| id | endpoint | load | outcome | p95 (vs baseline) | errors | delivered |",
                "|---|---|---|---|---|---|---|", *[_row(r, base_p95) for r in report.results], ""]
        for r in report.results:
            adj = r.experiment.adjustments
            out.append(f"- **{r.experiment.id}**: {r.experiment.hypothesis}" + (f" _(adjusted: {'; '.join(adj)})_" if adj else ""))
        out.append("")
    for title, items in (("Findings", report.findings), ("What was not covered", report.not_covered), ("Notes", report.notes)):
        if items:
            out += [f"## {title}", *[f"- {i}" for i in items], ""]
    return "\n".join(out)


def write_report(report: Report, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / "report.md", out_dir / "report.json"
    md.write_text(render_markdown(report), encoding="utf-8")
    js.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return md, js
