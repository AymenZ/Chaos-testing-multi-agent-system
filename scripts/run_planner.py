# scripts/run_planner.py
"""Run the planner on its own, for real, and save what it produced.

    python scripts/run_planner.py --offline     # facts from saved samples, no Docker needed
    python scripts/run_planner.py               # facts from the running sandbox (launch it first)

Needs ANTHROPIC_API_KEY (or `ant auth login`). Writes brief.json and audit.jsonl to runs/planner_<time>/.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

from swarm.audit import AuditLog  # noqa: E402
from swarm.manifest import load_manifest  # noqa: E402
from swarm.planner import Facts, PlannerError, plan  # noqa: E402
from swarm.preflight import make_sandbox  # noqa: E402
from swarm.tools.explorer import CodeJail  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--manifest", default="manifests/deployment_audit.yaml")
ap.add_argument("--offline", action="store_true", help="use tests/data samples instead of the sandbox")
args = ap.parse_args()

m = load_manifest(args.manifest)
out_dir = Path("runs") / time.strftime("planner_%Y%m%d_%H%M%S")
audit = AuditLog(out_dir / "audit.jsonl")
if args.offline:
    data = Path("tests/data")
    facts = Facts(lambda: json.loads((data / "openapi_sample.json").read_text(encoding="utf-8")),
                  lambda: json.loads((data / "db_schema_sample.json").read_text(encoding="utf-8")))
else:
    facts = Facts.from_sandbox(m, make_sandbox(m))

try:
    brief = plan(m, anthropic.Anthropic(), facts, CodeJail(m.target.code_path, audit), audit)
except PlannerError as e:
    print(f"planner failed: {e}\naudit: {out_dir / 'audit.jsonl'}")
    sys.exit(1)
except TypeError as e:
    if "authentication" not in str(e):
        raise
    sys.exit("No credentials found. Set ANTHROPIC_API_KEY (PowerShell: $env:ANTHROPIC_API_KEY = '...').")

(out_dir / "brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
calls = [e for e in audit.events if e["kind"] == "model_call"]
print(f"brief saved to {out_dir / 'brief.json'}")
print(f"{len(calls)} model calls, {sum(c['input_tokens'] for c in calls)} input / "
      f"{sum(c['output_tokens'] for c in calls)} output tokens, {sum(c['cache_read'] for c in calls)} read from cache")
print(f"{len(brief.output.endpoints)} endpoints, {len(brief.output.risks)} risks, {len(brief.dropped)} claims dropped by checks")
