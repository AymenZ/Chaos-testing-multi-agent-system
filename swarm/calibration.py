# swarm/calibration.py
"""Measure one endpoint's normal behaviour, then find its capacity. Plain code.

Used twice: by the Baseline node on the planner's baseline endpoint, and by the Calibrate node on
every endpoint the tester picks, so each experiment is sized against its own endpoint.

1. Run the endpoint at `normal_rps` several times (resetting the database before each) -> normal
   speed and its usual wobble.
2. Ramp up from there (normal_rps, x factor, ... up to the limit) and stop at the first unhealthy step
   -> capacity.
3. Ask check_capacity whether that number is usable.

Dependencies are passed in (`load`, `reset`), so the logic is testable without Docker and the same
code serves the live sandbox. Each measured run must pass the Validator; a technical flaw is rerun up
to the manifest's attempt limit, anything else stops calibration with the reasons.
"""
import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Callable

from swarm.baseline import (BaselineSummary, Capacity, CapacityCheck, CapacityContext, find_knee, ramp_rates,
                            step_healthy, check_capacity, summarize_baseline)
from swarm.brief import Brief, EndpointNote
from swarm.manifest import Manifest
from swarm.measurement import Measurement
from swarm.runner import LoadPlan
from swarm.validator import validate

LoadFn = Callable[[LoadPlan, str], Measurement]      # (plan, run name) -> measurement
MIN_DELIVERED = 0.9                                  # same bar the cards use


class CalibrationError(Exception):
    """Calibration could not produce trustworthy numbers (the reasons are in the message)."""


@dataclass
class BaselineRun:
    summary: BaselineSummary
    runs: list[Measurement]
    capacity: Capacity
    steps: list[Measurement]
    check: CapacityCheck
    endpoint: str = ""


def baseline_target(brief: Brief, health_path: str = "/health") -> tuple[str, EndpointNote]:
    """The first baseline target that does real work. The health check is skipped: it touches nothing,
    so its speed and capacity say nothing about the backend's actual endpoints."""
    notes = {n.key: n for n in brief.output.endpoints}
    for key in brief.output.baseline_targets:
        if key in notes and notes[key].recipe is not None and key.split(" ", 1)[1] != health_path:
            return key, notes[key]
    raise CalibrationError("the brief has no usable baseline target (the health check does not count)")


def endpoint_note(brief: Brief, key: str) -> EndpointNote:
    note = next((n for n in brief.output.endpoints if n.key == key), None)
    if note is None or note.recipe is None:
        raise CalibrationError(f"{key}: no recipe in the brief, so it cannot be measured")
    return note


def make_plan(m: Manifest, key: str, note: EndpointNote, rps: float) -> LoadPlan:
    """A flat load at `rps`: warmup at the same rate, then the measured hold."""
    method, path = key.split(" ", 1)
    r = note.recipe
    return LoadPlan(key, method, path, r.auth_role, r.query, r.json_body, r.form_body,
                    warmup_s=m.baseline.warmup_s, warmup_rps=rps, ramp_s=0, hold_s=m.baseline.measure_s,
                    target_rps=rps, abort_error_rate=0.5, max_vus=m.limits.max_vus, timeout_s=m.limits.request_timeout_s)


def _measure(m: Manifest, plan: LoadPlan, name: str, load: LoadFn, reset: Callable[[], None]) -> Measurement:
    reasons: list[str] = []
    for attempt in range(1, m.budgets.max_attempts + 1):
        reset()
        meas = load(plan, f"{name}-try{attempt}")
        verdict = validate(NS(target_rps=plan.target_rps, min_delivered=MIN_DELIVERED), meas, m.resources.host_cpu_limit_pct)
        if verdict.valid:
            return meas
        if verdict.flaw == "design":
            raise CalibrationError(f"{name}: the baseline request itself is wrong: {'; '.join(verdict.reasons)}")
        reasons = verdict.reasons                         # technical: rerun after a reset
    raise CalibrationError(f"{name}: could not get a clean run after {m.budgets.max_attempts} attempts: {'; '.join(reasons)}")


def measure_baseline(m: Manifest, brief: Brief, load: LoadFn, reset: Callable[[], None],
                     progress: Callable[[str], None] = lambda _: None, key: str | None = None) -> BaselineRun:
    """Measure `key`, or the planner's baseline endpoint when no key is given."""
    key, note = (key, endpoint_note(brief, key)) if key else baseline_target(brief, m.target.health_path)
    rules = m.rules.default

    runs = []
    for i in range(1, m.baseline.repeats + 1):
        progress(f"baseline run {i}/{m.baseline.repeats} at {m.baseline.normal_rps:g} rps on {key}")
        runs.append(_measure(m, make_plan(m, key, note, m.baseline.normal_rps), f"{slug(key)}-baseline-{i}", load, reset))
    summary = summarize_baseline(runs)

    steps: list[Measurement] = []
    for rate in ramp_rates(m.baseline.normal_rps, m.baseline.ramp_step_factor, m.limits.max_rps):
        progress(f"ramp step at {rate:g} rps on {key}")
        step = _measure(m, make_plan(m, key, note, rate), f"{slug(key)}-ramp-{rate:g}", load, reset)
        steps.append(step)
        if not step_healthy(step, summary, rules):
            break                                         # the first unhealthy step is all find_knee needs
    capacity = find_knee(steps, summary, rules)
    check = check_capacity(summary, capacity, m.limits, m.baseline.normal_rps)
    return BaselineRun(summary, runs, capacity, steps, check, key)


def calibrate(m: Manifest, brief: Brief, load: LoadFn, reset: Callable[[], None],
              progress: Callable[[str], None] = lambda _: None, key: str | None = None) -> BaselineRun:
    """measure_baseline, plus the one second go that check_capacity may ask for."""
    run = measure_baseline(m, brief, load, reset, progress, key)
    if run.check.outcome == "retry":
        progress(f"{run.endpoint}: the capacity check asked for another go ({run.check.reason})")
        run = measure_baseline(m, brief, load, reset, progress, run.endpoint)
        run = replace(run, check=check_capacity(run.summary, run.capacity, m.limits, m.baseline.normal_rps, retried=True))
    return run


def slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")       # 'GET /api/me' -> 'get-api-me'


# --- saving and loading what the later nodes need -------------------------------------------------

def save_baseline(run: BaselineRun, path: Path) -> None:
    data = {"endpoint": run.endpoint, "summary": asdict(run.summary), "capacity": asdict(run.capacity), "check": asdict(run.check),
            "runs": [r.model_dump(mode="json") for r in run.runs], "steps": [s.model_dump(mode="json") for s in run.steps]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_baseline(path: Path) -> tuple[BaselineSummary, CapacityCheck]:
    data = json.loads(path.read_text(encoding="utf-8"))
    c = data["check"]
    context = CapacityContext(**c["context"]) if c.get("context") else None
    return BaselineSummary(**data["summary"]), CapacityCheck(c["outcome"], c["reason"], context, c.get("noisy", False))
