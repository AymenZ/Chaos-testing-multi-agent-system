# scripts/run_baseline.py
"""Measure the baseline and the capacity for real, against the live sandbox. No LLM calls.

    python scripts/run_baseline.py                  # uses the newest planner brief's baseline target

Takes a while: every run resets the database first, warms up, and measures for the manifest's
baseline.measure_s. Launch the sandbox first (see README). Writes runs/baseline_<time>/baseline.json.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swarm.baseline import ramp_rates  # noqa: E402
from swarm.brief import Brief  # noqa: E402
from swarm.calibration import CalibrationError, baseline_target, make_plan, measure_baseline, save_baseline  # noqa: E402
from swarm.manifest import load_manifest  # noqa: E402
from swarm.preflight import make_sandbox  # noqa: E402
from swarm.runner import RunnerError, run_load, smoke_probe  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--manifest", default="manifests/deployment_audit.yaml")
ap.add_argument("--brief", help="path to a brief.json; default: the newest planner run")
args = ap.parse_args()

m = load_manifest(args.manifest)
brief_path = Path(args.brief) if args.brief else max(Path("runs").glob("planner_*/brief.json"), default=None)
if brief_path is None or not brief_path.exists():
    sys.exit("No brief found. Run scripts/run_planner.py first, or pass --brief.")
brief = Brief.model_validate_json(brief_path.read_text(encoding="utf-8"))

sandbox = make_sandbox(m)
health = str(m.target.base_url).rstrip("/") + m.target.health_path
out_dir = Path("runs") / time.strftime("baseline_%Y%m%d_%H%M%S")
key, note = baseline_target(brief, m.target.health_path)
per_run = 10 + m.baseline.warmup_s + 1 + m.baseline.measure_s + 5
runs_max = m.baseline.repeats + len(ramp_rates(m.baseline.normal_rps, m.baseline.ramp_step_factor, m.limits.max_rps))
print(f"target: {key}   usual traffic: {m.baseline.normal_rps:g} rps   brief: {brief_path}")
print(f"up to {runs_max} runs of about {per_run}s each: roughly {m.baseline.repeats * per_run // 60} to {runs_max * per_run // 60} minutes\n")

start = time.monotonic()
try:
    smoke_probe(m, sandbox, make_plan(m, key, note, m.baseline.normal_rps))
    result = measure_baseline(
        m, brief, lambda plan, name: run_load(m, sandbox, plan, out_dir / name), lambda: sandbox.reset(health),
        progress=lambda msg: print(f"[{(time.monotonic() - start) / 60:5.1f} min] {msg}", flush=True))
except (CalibrationError, RunnerError) as e:
    sys.exit(f"baseline failed: {e}")

save_baseline(result, out_dir / "baseline.json")
s, c = result.summary, result.check
print(f"\nBaseline at {m.baseline.normal_rps:g} rps: p95 {s.p95_median_ms:.1f} ms (range {s.p95_min_ms:.1f}-{s.p95_max_ms:.1f}), "
      f"errors {s.error_rate_median:.2%}, spread {s.spread:.0%}{'  NOISY' if s.noisy else ''}")
print(f"Ramp: {[round(x.target_rps) for x in result.steps]} rps -> capacity {result.capacity.status}"
      + (f" at {result.capacity.rps:.0f} rps" if result.capacity.rps else ""))
print(f"Check: {c.outcome.upper()}: {c.reason}")
if c.context:
    print(f"The limits allow up to {c.context.max_multiple}x capacity.")
print(f"\nfiles: {out_dir}")
