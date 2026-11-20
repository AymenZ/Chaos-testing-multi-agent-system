# swarm/audit.py
"""Append-only record of what the guards did: denied reads, dropped claims, budget hits.
Model calls themselves are better viewed in a tracing tool; this is the offline, testable record."""
import json
import time
from pathlib import Path
from typing import Callable


class AuditLog:
    def __init__(self, path: Path | None = None, echo: Callable[[dict], None] | None = None):
        self.path = Path(path) if path else None
        self._echo = echo                       # e.g. print_event, to watch a run live
        self.events: list[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields) -> None:
        record = {"ts": round(time.time(), 3), "kind": kind, **fields}
        self.events.append(record)
        if self._echo:
            self._echo(record)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


def print_event(r: dict) -> None:
    """One readable line per event, for watching a real run."""
    kind = r["kind"]
    if kind == "model_request":
        print(f"  step {r['step']}: asking the model...", flush=True)
    elif kind == "model_call":
        step = r.get("step", r.get("attempt"))
        print(f"  step {step}: model replied ({r['input_tokens']} in / {r['output_tokens']} out tokens)", flush=True)
    elif kind == "tool_call":
        print(f"    tool: {r['name']} {r['args'][:90]}", flush=True)
    else:
        detail = {k: v for k, v in r.items() if k not in ("ts", "kind")}
        print(f"  [{kind}] {str(detail)[:200]}", flush=True)
