# swarm/audit.py
"""Append-only record of what the guards did: denied reads, dropped claims, budget hits.
Model calls themselves are better viewed in a tracing tool; this is the offline, testable record."""
import json
import time
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.events: list[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields) -> None:
        record = {"ts": round(time.time(), 3), "kind": kind, **fields}
        self.events.append(record)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]
