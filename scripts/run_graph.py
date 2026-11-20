# scripts/run_graph.py
"""Run the whole v1 graph end to end against the live sandbox and write the report.

    python scripts/run_graph.py

Launch the sandbox first (see README) and put the API key for llm.provider in .env. Calls the model
for the planner (up to llm.planner_max_steps) and the tester (up to llm.tester_max_calls per round),
and takes roughly half an hour because of the baseline. Everything lands in runs/graph_<time>/.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from swarm.audit import AuditLog, print_event  # noqa: E402
from swarm.graph import Deps, run_graph  # noqa: E402
from swarm.llm import MissingKeyError, make_client  # noqa: E402
from swarm.manifest import load_manifest  # noqa: E402
from swarm.preflight import make_sandbox  # noqa: E402

load_dotenv()
ap = argparse.ArgumentParser()
ap.add_argument("--manifest", default="manifests/deployment_audit.yaml")
args = ap.parse_args()

m = load_manifest(args.manifest)
try:
    client = make_client(m)
except MissingKeyError as e:
    sys.exit(str(e))

out_dir = Path("runs") / time.strftime("graph_%Y%m%d_%H%M%S")
start = time.monotonic()


def say(msg: str) -> None:
    print(f"[{(time.monotonic() - start) / 60:5.1f} min] {msg}", flush=True)


deps = Deps(m, make_sandbox(m), client, AuditLog(out_dir / "audit.jsonl", echo=print_event), out_dir, say)
report = run_graph(deps)
print(f"\n{report.label.upper()}: {report.headline}\nfiles: {out_dir}")
