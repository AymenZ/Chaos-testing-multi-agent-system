# scripts/run_experiments.py
"""Run the tester's experiments for real and write the report. No LLM calls.

    python scripts/run_experiments.py               # newest experiments.json, baseline.json and brief

A plain sequential harness, NOT the graph: it does the minimum routing needed to try the nodes together
(rerun technical flaws, confirm a clear fail with one repeat, median of three for borderline results).
A design flaw is recorded as "could not test"; in the full graph it goes back to the tester.
Writes runs/experiments_<time>/report.md and report.json.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swarm.brief import Brief  # noqa: E402
from swarm.calibration import load_baseline  # noqa: E402
from swarm.guardrail import Experiment  # noqa: E402
from swarm.manifest import load_manifest  # noqa: E402
from swarm.preflight import make_sandbox  # noqa: E402
from swarm.harness import run_one  # noqa: E402
from swarm.report import build_report, write_report  # noqa: E402
from swarm.runner import RunnerError, run_experiment  # noqa: E402
from swarm.validator import validate  # noqa: E402


def newest(pattern: str, given: str | None, what: str, hint: str) -> Path:
    path = Path(given) if given else max(Path("runs").glob(pattern), default=None)
    if path is None or not path.exists():
        sys.exit(f"No {what} found. {hint}")
    return path


ap = argparse.ArgumentParser()
ap.add_argument("--manifest", default="manifests/deployment_audit.yaml")
ap.add_argument("--experiments", help="path to experiments.json")
ap.add_argument("--baseline", help="path to baseline.json")
ap.add_argument("--brief", help="path to brief.json")
args = ap.parse_args()

m = load_manifest(args.manifest)
exp_path = newest("tester_*/experiments.json", args.experiments, "experiments", "Run scripts/run_tester.py first.")
base_path = newest("baseline_*/baseline.json", args.baseline, "baseline", "Run scripts/run_baseline.py first.")
brief_path = newest("planner_*/brief.json", args.brief, "brief", "Run scripts/run_planner.py first.")
experiments = [Experiment.model_validate(e) for e in json.loads(exp_path.read_text(encoding="utf-8"))]
summary, check = load_baseline(base_path)
brief = Brief.model_validate_json(brief_path.read_text(encoding="utf-8"))
rules, limit = m.rules.default, m.resources.host_cpu_limit_pct

sandbox = make_sandbox(m)
health = str(m.target.base_url).rstrip("/") + m.target.health_path
out_dir = Path("runs") / time.strftime("experiments_%Y%m%d_%H%M%S")
start = time.monotonic()


def say(msg: str) -> None:
    print(f"[{(time.monotonic() - start) / 60:5.1f} min] {msg}", flush=True)


def one_valid_run(exp: Experiment, name: str):
    """One run through the Validator. Returns (measurement or None, reasons, validity or None)."""
    try:
        meas = run_experiment(m, sandbox, exp, out_dir / name, health)
    except RunnerError as e:
        return None, [str(e)], None
    v = validate(exp, meas, limit)
    return (meas if v.valid else None), v.reasons, v


results = []
if check.context is not None:                       # no usable capacity: nothing to run, the report says why
    results = [run_one(m, e, summary, one_valid_run, say) for e in experiments]
report = build_report(brief=brief, baseline=summary, capacity=check, results=results)
md, js = write_report(report, out_dir)
print(f"\n{report.label.upper()}: {report.headline}\nreport: {md}")
