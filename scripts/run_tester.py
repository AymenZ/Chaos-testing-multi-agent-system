# scripts/run_tester.py
"""Run the request tester (and the guardrail after it) on a real planner brief and the real baseline.

    python scripts/run_tester.py                          # newest planner brief + newest baseline
    python scripts/run_tester.py --capacity-rps 120       # skip the baseline with a stand-in capacity

Needs the API key for the manifest's llm.provider in .env. Writes cards.json and experiments.json to
runs/tester_<time>/ and prints the resolved experiments.
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
from swarm.baseline import CapacityContext  # noqa: E402
from swarm.brief import Brief  # noqa: E402
from swarm.calibration import load_baseline  # noqa: E402
from swarm.guardrail import apply_guardrail  # noqa: E402
from swarm.llm import MissingKeyError, make_client  # noqa: E402
from swarm.manifest import load_manifest  # noqa: E402
from swarm.tester import TesterError, design_cards  # noqa: E402

load_dotenv()
ap = argparse.ArgumentParser()
ap.add_argument("--manifest", default="manifests/deployment_audit.yaml")
ap.add_argument("--brief", help="path to a brief.json; default: the newest planner run")
ap.add_argument("--baseline", help="path to a baseline.json; default: the newest baseline run")
ap.add_argument("--capacity-rps", type=float, help="use this stand-in capacity instead of a measured baseline")
args = ap.parse_args()

m = load_manifest(args.manifest)
brief_path = Path(args.brief) if args.brief else max(Path("runs").glob("planner_*/brief.json"), default=None)
if brief_path is None or not brief_path.exists():
    sys.exit("No brief found. Run scripts/run_planner.py first, or pass --brief.")
brief = Brief.model_validate_json(brief_path.read_text(encoding="utf-8"))

if args.capacity_rps:
    capacity = CapacityContext(args.capacity_rps, "exact", round(m.limits.max_rps / args.capacity_rps, 2))
    print(f"capacity: STAND-IN {capacity}")
else:
    base_path = Path(args.baseline) if args.baseline else max(Path("runs").glob("baseline_*/baseline.json"), default=None)
    if base_path is None or not base_path.exists():
        sys.exit("No baseline found. Run scripts/run_baseline.py first, or pass --capacity-rps for a stand-in.")
    _, check = load_baseline(base_path)
    if check.context is None:
        sys.exit(f"The baseline has no usable capacity ({check.outcome}): {check.reason}")
    capacity = check.context
    print(f"capacity: measured {capacity} from {base_path}")

try:
    client = make_client(m)
except MissingKeyError as e:
    sys.exit(str(e))
out_dir = Path("runs") / time.strftime("tester_%Y%m%d_%H%M%S")
audit = AuditLog(out_dir / "audit.jsonl", echo=print_event)
print(f"brief: {brief_path}\ntester: {m.llm.provider} / {m.llm.tester_model}")
try:
    cards = design_cards(m, client, brief, capacity, audit)
except (TesterError, GeminiError) as e:
    sys.exit(f"tester failed: {e}\naudit: {out_dir / 'audit.jsonl'}")

result = apply_guardrail(cards, brief, capacity, m)
(out_dir / "cards.json").write_text(json.dumps([c.model_dump(mode="json") for c in cards], indent=2), encoding="utf-8")
(out_dir / "experiments.json").write_text(json.dumps([e.model_dump(mode="json") for e in result.experiments], indent=2), encoding="utf-8")

print(f"\n{len(cards)} card(s) -> {len(result.experiments)} experiment(s), {len(result.rejected)} rejected\n")
for e in result.experiments:
    print(f"- {e.id}: {e.endpoint} at {e.load_multiple:g}x ({e.target_rps:.0f} rps), ramp {e.ramp_s}s, hold {e.hold_s}s, signals {[s.value for s in e.signals]}")
    print(f"    {e.hypothesis}")
    for a in e.adjustments:
        print(f"    adjusted: {a}")
for r in result.rejected:
    print(f"- REJECTED {r.card_id}: {r.reason}")
print(f"\nfiles: {out_dir}")
