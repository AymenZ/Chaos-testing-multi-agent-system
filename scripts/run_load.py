# scripts/run_load.py
"""Run one load plan against the live sandbox and print the Measurement. The first real check of the
runner; no agents involved. Launch the sandbox first (see README), and keep it modest to begin with:

    python scripts/run_load.py --method GET --path /api/me --role user --rps 20 --hold 20

The first run pulls the k6 image. Output files go to runs/load_<time>/.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swarm.manifest import load_manifest  # noqa: E402
from swarm.preflight import make_sandbox  # noqa: E402
from swarm.runner import LoadPlan, RunnerError, run_load, smoke_probe  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--manifest", default="manifests/deployment_audit.yaml")
ap.add_argument("--method", default="GET")
ap.add_argument("--path", required=True)
ap.add_argument("--role", default="none", choices=["none", "user", "admin"])
ap.add_argument("--rps", type=float, required=True)
ap.add_argument("--hold", type=int, default=20, help="seconds measured")
ap.add_argument("--ramp", type=int, default=0)
ap.add_argument("--no-reset", action="store_true", help="skip resetting the database first")
args = ap.parse_args()

m = load_manifest(args.manifest)
sandbox = make_sandbox(m)
health = str(m.target.base_url).rstrip("/") + m.target.health_path
plan = LoadPlan(f"{args.method} {args.path}", args.method, args.path, args.role, {}, None, None,
                m.baseline.warmup_s, args.rps * 0.5, args.ramp, args.hold, args.rps, 0.5,
                m.limits.max_vus, m.limits.request_timeout_s)
out_dir = Path("runs") / time.strftime("load_%Y%m%d_%H%M%S")
try:
    if not args.no_reset:
        sandbox.reset(health)
    smoke_probe(m, sandbox, plan)
    print("smoke probe ok; running load...")
    result = run_load(m, sandbox, plan, out_dir)
except RunnerError as e:
    sys.exit(f"runner error: {e}")
print(result.model_dump_json(indent=2))
print(f"files: {out_dir}")
