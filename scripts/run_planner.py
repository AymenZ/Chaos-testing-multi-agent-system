# scripts/run_planner.py
"""Run the planner on its own and save what it produced.

    python scripts/run_planner.py               # facts from the live sandbox (launch it first)
    python scripts/run_planner.py --offline     # facts from saved samples, no Docker needed

Needs the API key for the manifest's llm.provider in .env (GEMINI_API_KEY or ANTHROPIC_API_KEY).
Writes brief.json and audit.jsonl to runs/planner_<time>/.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from adapters.gemini import GeminiError  # noqa: E402
from swarm.audit import AuditLog, print_event  # noqa: E402
from swarm.llm import MissingKeyError, make_client  # noqa: E402
from swarm.manifest import load_manifest  # noqa: E402
from swarm.planner import Facts, PlannerError, plan  # noqa: E402
from swarm.preflight import make_sandbox  # noqa: E402
from swarm.tools.explorer import CodeJail  # noqa: E402

load_dotenv()
ap = argparse.ArgumentParser()
ap.add_argument("--manifest", default="manifests/deployment_audit.yaml")
ap.add_argument("--offline", action="store_true", help="use tests/data samples instead of the sandbox")
args = ap.parse_args()

m = load_manifest(args.manifest)
try:
    client = make_client(m)
except MissingKeyError as e:
    sys.exit(str(e))

out_dir = Path("runs") / time.strftime("planner_%Y%m%d_%H%M%S")
audit = AuditLog(out_dir / "audit.jsonl", echo=print_event)
if args.offline:
    data = Path("tests/data")
    facts = Facts(lambda: json.loads((data / "openapi_sample.json").read_text(encoding="utf-8")),
                  lambda: json.loads((data / "db_schema_sample.json").read_text(encoding="utf-8")))
else:
    facts = Facts.from_sandbox(m, make_sandbox(m))

lim = m.llm.limits.get(m.llm.planner_model)
print(f"planner: {m.llm.provider} / {m.llm.planner_model}, up to {m.llm.planner_max_steps} calls"
      + (f" (limits: {lim.rpm} per minute, {lim.rpd} per day)" if lim else ""))
try:
    brief = plan(m, client, facts, CodeJail(m.target.code_path, audit), audit)
except (PlannerError, GeminiError) as e:
    sys.exit(f"planner failed: {e}\naudit: {out_dir / 'audit.jsonl'}")

(out_dir / "brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
calls = [e for e in audit.events if e["kind"] == "model_call"]
print(f"brief saved to {out_dir / 'brief.json'}")
print(f"{len(calls)} model calls, {sum(c['input_tokens'] for c in calls)} input / "
      f"{sum(c['output_tokens'] for c in calls)} output tokens, {sum(c['cache_read'] for c in calls)} read from cache")
print(f"{len(brief.output.endpoints)} endpoints, {len(brief.output.risks)} risks, {len(brief.dropped)} claims dropped by checks")
for d in brief.dropped:
    print(f"  dropped: {d}")
if hasattr(client, "usage_today"):
    used, daily = client.usage_today(m.llm.planner_model)
    print(f"{m.llm.planner_model}: {used} of {daily} calls used today (counted by this tool)")
