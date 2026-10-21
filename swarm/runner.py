# swarm/runner.py
"""Runner: executes load against the sandbox and returns a Measurement. Plain code, never an agent.

One primitive, `run_load(plan)`: warm up, ramp, hold at a constant request rate, measure the hold.
Baseline runs, capacity-ramp steps and experiments are all the same thing with different numbers.

How: write config.json next to the fixed k6 script, run k6 as a one-off container on the sandbox's
isolated network, read the summary k6 writes, and add machine peaks from the sampler.
"""
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from adapters.hostmetrics import Peaks, Sampler
from adapters.sandbox import Sandbox, SandboxError
from swarm.guardrail import Experiment
from swarm.manifest import Manifest
from swarm.measurement import Measurement

K6_SCRIPT = Path(__file__).parent / "k6" / "script.js"
K6_NAME = "chaos_k6"
# Statuses k6 is asked to count individually; anything else lands in the "other" bucket (key -1).
STATUSES = [0, 200, 201, 202, 204, 400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504]
OTHER = -1
PLACEHOLDER = re.compile(r"\{\{\s*(user|admin)\.(email|password)\s*\}\}")


class RunnerError(Exception):
    """The run could not be carried out (setup problem). Not a verdict about the backend."""


@dataclass(frozen=True)
class LoadPlan:
    key: str
    method: str
    path: str
    auth_role: str
    query: dict
    json_body: dict | None
    form_body: dict | None
    warmup_s: int
    warmup_rps: float
    ramp_s: int
    hold_s: int
    target_rps: float
    abort_error_rate: float
    max_vus: int
    timeout_s: float

    @property
    def total_s(self) -> int:
        return self.warmup_s + max(self.ramp_s, 1) + self.hold_s       # the script always ramps for at least 1 s


def plan_from_experiment(e: Experiment) -> LoadPlan:
    return LoadPlan(e.endpoint, e.method, e.path, e.auth_role, e.query, e.json_body, e.form_body, e.warmup_s,
                    e.warmup_rps, e.ramp_s, e.hold_s, e.target_rps, e.abort_error_rate, e.max_vus, e.timeout_s)


# --- config for the fixed k6 script -----------------------------------------------------------

def _credentials(m: Manifest) -> dict[str, dict[str, str]]:
    creds: dict[str, dict[str, str]] = {}
    for u in m.auth.seed_users:
        creds.setdefault(u.role, {"email": u.email, "password": u.password})
    return creds


def resolve_placeholders(obj: Any, creds: dict[str, dict[str, str]]) -> Any:
    """Fill {{user.email}} style placeholders. The last moment credentials are touched: never earlier."""
    if isinstance(obj, str):
        def sub(match: re.Match) -> str:
            role, field = match.groups()
            if role not in creds:
                raise RunnerError(f"recipe needs a '{role}' account but the manifest has none")
            return creds[role][field]
        return PLACEHOLDER.sub(sub, obj)
    if isinstance(obj, dict):
        return {k: resolve_placeholders(v, creds) for k, v in obj.items()}
    return obj


def _login_body(m: Manifest, who: dict[str, str]) -> dict:
    if m.auth.login_encoding == "form":
        return {"username": who["email"], "password": who["password"]}
    return {"email": who["email"], "password": who["password"]}


def build_config(plan: LoadPlan, m: Manifest) -> dict:
    base = str(m.target.base_url).rstrip("/")
    creds = _credentials(m)
    login = None
    if plan.auth_role != "none":
        if plan.auth_role not in creds:
            raise RunnerError(f"experiment needs a '{plan.auth_role}' account but the manifest has none")
        login = {"url": base + m.auth.login_path, "encoding": m.auth.login_encoding,
                 "token_field": m.auth.token_field, "body": _login_body(m, creds[plan.auth_role])}
    query = "?" + urlencode(plan.query) if plan.query else ""
    return {
        "method": plan.method, "url": base + plan.path + query, "login": login,
        "json_body": resolve_placeholders(plan.json_body, creds),
        "form_body": resolve_placeholders(plan.form_body, creds),
        "warmup_s": plan.warmup_s, "warmup_rps": max(round(plan.warmup_rps), 1),
        "ramp_s": plan.ramp_s, "hold_s": plan.hold_s, "target_rps": max(round(plan.target_rps), 1),
        "pre_vus": min(plan.max_vus, max(10, math.ceil(plan.target_rps * 0.1))), "max_vus": plan.max_vus,
        "timeout_s": plan.timeout_s, "abort_error_rate": plan.abort_error_rate, "statuses": STATUSES,
    }


# --- reading k6's summary ---------------------------------------------------------------------

def _v(metrics: dict, name: str, key: str, default: float = 0.0) -> float:
    return float(metrics.get(name, {}).get("values", {}).get(key, default))


def parse_result(raw: dict, plan: LoadPlan, exit_code: int) -> Measurement:
    """Numbers come from the hold phase. If the run was aborted before reaching it, from the whole run."""
    metrics = raw.get("metrics", {})
    failed_rate = _v(metrics, "http_req_failed", "rate")
    aborted = exit_code == 99 and failed_rate >= plan.abort_error_rate * 0.999
    hold_total = _v(metrics, "http_reqs{phase:hold}", "count")
    whole_run = aborted or hold_total == 0
    tag = "" if whole_run else "{phase:hold}"

    total = int(_v(metrics, f"http_reqs{tag}", "count"))
    counts = {s: int(_v(metrics, f"http_reqs{{{'phase:hold,' if not whole_run else ''}status:{s}}}", "count"))
              for s in STATUSES}
    counts = {s: n for s, n in counts.items() if n > 0}
    if total - sum(counts.values()) > 0:
        counts[OTHER] = total - sum(counts.values())
    errors = sum(n for s, n in counts.items() if s == 0 or s >= 500 or s == OTHER)

    rate = _v(metrics, "http_reqs", "rate")
    duration = (total / rate if whole_run and rate > 0 else plan.hold_s) or plan.hold_s
    return Measurement(
        target_rps=plan.target_rps, duration_s=duration, requests=total,
        achieved_rps=total / duration if duration else 0.0,
        p50_ms=_v(metrics, f"http_req_duration{tag}", "med"), p95_ms=_v(metrics, f"http_req_duration{tag}", "p(95)"),
        p99_ms=_v(metrics, f"http_req_duration{tag}", "p(99)"),
        error_rate=errors / total if total else 0.0, status_counts=counts, timeouts=counts.get(0, 0),
        dropped_iterations=int(_v(metrics, "dropped_iterations", "count")),
        aborted=aborted, abort_reason=f"error rate reached {failed_rate:.0%} (limit {plan.abort_error_rate:.0%})" if aborted else None,
    )


def _with_machine_peaks(meas: Measurement, peaks: Peaks) -> Measurement:
    """Machine peaks plus the catalog signals the verdict checks (names match brief.Signal)."""
    meas.loadgen_cpu_max_pct = peaks.loadgen_cpu_pct
    meas.host_cpu_max_pct = peaks.host_cpu_pct
    meas.signals = {"request_latency_p95": meas.p95_ms, "error_rate": meas.error_rate,
                    "achieved_request_rate": meas.achieved_rps, "api_cpu": peaks.api_cpu_pct,
                    "db_cpu": peaks.db_cpu_pct, "db_connections": peaks.db_connections}
    return meas


# --- running ----------------------------------------------------------------------------------

def run_load(m: Manifest, sandbox: Sandbox, plan: LoadPlan, out_dir: Path, sampler: Any = None) -> Measurement:
    """Run one load plan against the (already reset) sandbox. Files for the run are kept in out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(json.dumps(build_config(plan, m), indent=2), encoding="utf-8")
    shutil.copyfile(K6_SCRIPT, out_dir / "script.js")
    (out_dir / "result.json").unlink(missing_ok=True)

    sampler = sampler or Sampler(sandbox, K6_NAME, {"api": m.resources.api_cpus, "db": m.resources.db_cpus,
                                                    "loadgen": m.resources.loadgen_cpus})
    sampler.start()
    try:
        proc = sandbox.run_tool("k6", "run", "/work/script.js", mount=out_dir, name=K6_NAME,
                                timeout=plan.total_s + int(plan.timeout_s) + 60)
    finally:
        peaks = sampler.stop()
    (out_dir / "k6.stdout.txt").write_text(proc.stdout or "", encoding="utf-8")
    (out_dir / "k6.stderr.txt").write_text(proc.stderr or "", encoding="utf-8")

    if proc.returncode not in (0, 99):
        raise RunnerError(f"k6 failed (exit {proc.returncode}): {(proc.stderr or '').strip()[-400:]}")
    try:
        raw = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RunnerError(f"k6 produced no readable summary: {e}") from e
    return _with_machine_peaks(parse_result(raw, plan, proc.returncode), peaks)


def smoke_probe(m: Manifest, sandbox: Sandbox, plan: LoadPlan) -> None:
    """One real request before spending minutes of load: a wrong login or recipe would otherwise
    make the whole run a pile of 401s that measures nothing."""
    cfg = build_config(plan, m)
    headers: dict = {}
    if cfg["login"]:
        lg = cfg["login"]
        body_kw = {"form": lg["body"]} if lg["encoding"] == "form" else {"json_body": lg["body"]}
        status, body = sandbox.request("POST", lg["url"], **body_kw)
        try:
            token = json.loads(body).get(lg["token_field"]) if status == 200 else None
        except ValueError:
            token = None
        if not token:
            raise RunnerError(f"smoke probe: login returned {status}")
        headers["Authorization"] = f"Bearer {token}"
    if cfg["form_body"] is not None:
        body_kw = {"form": cfg["form_body"]}
    else:
        body_kw = {"json_body": cfg["json_body"]} if cfg["json_body"] is not None else {}
    status, _ = sandbox.request(cfg["method"], cfg["url"], headers=headers, **body_kw)
    if not 200 <= status < 400:
        raise RunnerError(f"smoke probe: {plan.key} returned {status}")


def run_experiment(m: Manifest, sandbox: Sandbox, exp: Experiment, out_dir: Path, health_url: str,
                   sampler: Any = None) -> Measurement:
    """Reset to the clean copy, prove the request works, then run the load."""
    plan = plan_from_experiment(exp)
    sandbox.reset(health_url)
    smoke_probe(m, sandbox, plan)
    return run_load(m, sandbox, plan, out_dir, sampler)
