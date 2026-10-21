# adapters/hostmetrics.py
"""Samples machine and container load while an experiment runs, so the Validator can tell
"the backend struggled" from "our own machine was the bottleneck", and so the Verdict can check
that a card's mechanism signals actually appeared. Reports peaks, not series."""
import json
import subprocess
import threading
from dataclasses import dataclass

import psutil

from adapters.sandbox import Sandbox


@dataclass
class Peaks:
    host_cpu_pct: float = 0.0       # whole machine
    loadgen_cpu_pct: float = 0.0    # the load generator, as % of its own CPU allowance
    api_cpu_pct: float = 0.0
    db_cpu_pct: float = 0.0
    db_connections: float = 0.0


def parse_docker_stats(text: str) -> dict[str, float]:
    """`docker stats --format '{{json .}}'` output -> {container name: cpu %}. Bad lines are skipped."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        try:
            row = json.loads(line)
            out[row["Name"]] = float(row["CPUPerc"].rstrip("%"))
        except (ValueError, KeyError, TypeError):
            continue
    return out


class Sampler:
    """Background sampler. Docker reports 100% per allowed core, so each container's figure is
    divided by its cpus to give "% of what it was allowed"."""

    def __init__(self, sandbox: Sandbox, loadgen_name: str, cpus: dict[str, float], interval_s: float = 2.0):
        self._sandbox, self._loadgen, self._cpus, self._interval = sandbox, loadgen_name, cpus, interval_s
        self._api = sandbox.container_name(sandbox.api)
        self._db = sandbox.container_name(sandbox.db)
        self.peaks = Peaks()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        psutil.cpu_percent(interval=None)        # the first reading is meaningless; prime it
        self._thread.start()

    def stop(self) -> Peaks:
        self._stop.set()
        self._thread.join(timeout=self._interval * 3 + 5)
        return self.peaks

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample_once()
            self._stop.wait(self._interval)

    def sample_once(self) -> None:
        p = self.peaks
        p.host_cpu_pct = max(p.host_cpu_pct, psutil.cpu_percent(interval=None))
        try:
            cpu = parse_docker_stats(self._docker_stats())
            p.api_cpu_pct = max(p.api_cpu_pct, cpu.get(self._api, 0.0) / self._cpus["api"])
            p.db_cpu_pct = max(p.db_cpu_pct, cpu.get(self._db, 0.0) / self._cpus["db"])
            p.loadgen_cpu_pct = max(p.loadgen_cpu_pct, cpu.get(self._loadgen, 0.0) / self._cpus["loadgen"])
        except (OSError, subprocess.SubprocessError):
            pass                                  # a missed sample is fine; the peak keeps what it saw
        try:
            p.db_connections = max(p.db_connections, self._connections())
        except Exception:                         # the database may be busy; never let sampling break a run
            pass

    def _docker_stats(self) -> str:
        r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}",
                            self._api, self._db, self._loadgen], capture_output=True, text=True,
                           encoding="utf-8", timeout=20)
        return r.stdout

    def _connections(self) -> float:
        return float(self._sandbox.psql("SELECT count(*) FROM pg_stat_activity WHERE datname = 'app'").strip())
