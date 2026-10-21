# tests/test_runner.py
"""The runner against fakes: no Docker, no k6. What it can't prove (k6's real summary format) is
exactly what scripts/run_load.py is for."""
import json
from types import SimpleNamespace as NS

import pytest

from adapters.hostmetrics import Peaks, Sampler, parse_docker_stats
from swarm.baseline import CapacityContext
from swarm.card import Card
from swarm.guardrail import apply_guardrail
from swarm.manifest import load_manifest
from swarm.runner import (K6_SCRIPT, LoadPlan, RunnerError, build_config, parse_result, plan_from_experiment,
                          resolve_placeholders, run_experiment, run_load, smoke_probe)
from tests.test_tester import ME, card, make_brief

HEALTH = "http://api:8000/health"


@pytest.fixture
def m():
    return load_manifest("manifests/deployment_audit.yaml")


def make_plan(**over) -> LoadPlan:
    d = dict(key=ME, method="GET", path="/api/me", auth_role="user", query={}, json_body=None, form_body=None,
             warmup_s=15, warmup_rps=50, ramp_s=0, hold_s=60, target_rps=100, abort_error_rate=0.5,
             max_vus=1000, timeout_s=5)
    d.update(over)
    return LoadPlan(**d)


# --- fakes -------------------------------------------------------------------------

def counter(n, rate=0.0):
    return {"values": {"count": n, "rate": rate}}


def raw_result(hold=None, whole=None, p95=40.0, failed_rate=0.0, dropped=None, whole_rate=100.0) -> dict:
    metrics: dict = {"http_req_failed": {"values": {"rate": failed_rate}}}
    if dropped is not None:
        metrics["dropped_iterations"] = counter(dropped)
    for counts, tag, key in ((hold, "{phase:hold}", "phase:hold,"), (whole, "", "")):
        if counts is None:
            continue
        total = counts.get("total", sum(v for k, v in counts.items() if k != "total"))
        metrics[f"http_reqs{tag}"] = counter(total, whole_rate if not tag else 0)
        metrics[f"http_req_duration{tag}"] = {"values": {"med": 30.0, "p(95)": p95, "p(99)": 150.0, "max": 400.0}}
        for status, n in counts.items():
            if status != "total":
                metrics[f"http_reqs{{{key}status:{status}}}"] = counter(n)
    return {"metrics": metrics}


class FakeSampler:
    def start(self): pass
    def stop(self): return Peaks(host_cpu_pct=55, loadgen_cpu_pct=40, api_cpu_pct=90, db_cpu_pct=30, db_connections=17)


class FakeSandbox:
    api, db, project = "api", "db", "chaos_target"

    def __init__(self, result=None, exit_code=0, login_status=200, endpoint_status=200):
        self.calls, self.requests = [], []
        self.result, self.exit_code, self.login_status, self.endpoint_status = result, exit_code, login_status, endpoint_status

    def reset(self, url): self.calls.append("reset")

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        if url.endswith("/api/login") and method == "POST" and kw.get("form", {}).get("username", "").endswith("example.com"):
            return self.login_status, json.dumps({"access_token": "tok"}) if self.login_status == 200 else "{}"
        return self.endpoint_status, "{}"

    def run_tool(self, service, *args, mount, name, timeout):
        self.calls.append("k6")
        self.last_mount, self.last_timeout = mount, timeout
        if self.result is not None:
            (mount / "result.json").write_text(json.dumps(self.result), encoding="utf-8")
        return NS(returncode=self.exit_code, stdout="", stderr="script exploded")


# --- config ------------------------------------------------------------------------

def test_config_for_an_authed_request(m):
    cfg = build_config(make_plan(path="/api/users", query={"limit": 100}, auth_role="admin", target_rps=99.6), m)
    assert cfg["url"] == "http://api:8000/api/users?limit=100" and cfg["method"] == "GET"
    assert cfg["login"]["body"] == {"username": "admin0@example.com", "password": "sandbox-pass-admin"}
    assert cfg["login"]["url"].endswith("/api/login") and cfg["login"]["token_field"] == "access_token"
    assert cfg["target_rps"] == 100 and cfg["warmup_rps"] == 50 and cfg["pre_vus"] == 10 and cfg["max_vus"] == 1000


def test_unauthenticated_plan_has_no_login(m):
    assert build_config(make_plan(auth_role="none"), m)["login"] is None


def test_placeholders_are_resolved_only_when_building_the_config(m):
    body = {"username": "{{user.email}}", "password": "{{ user.password }}", "other": "{{admin.email}}"}
    cfg = build_config(make_plan(auth_role="none", method="POST", path="/api/login", form_body=body), m)
    assert cfg["form_body"] == {"username": "user0@example.com", "password": "sandbox-pass-0", "other": "admin0@example.com"}
    assert body["username"] == "{{user.email}}"                       # the plan itself is untouched


def test_a_missing_account_is_a_runner_error(m):
    m.auth.seed_users = [u for u in m.auth.seed_users if u.role == "user"]
    with pytest.raises(RunnerError, match="admin"):
        build_config(make_plan(auth_role="admin"), m)
    with pytest.raises(RunnerError, match="admin"):
        resolve_placeholders({"x": "{{admin.email}}"}, {"user": {"email": "a", "password": "b"}})


def test_the_k6_script_is_one_fixed_file_with_no_hardcoded_target():
    js = K6_SCRIPT.read_text(encoding="utf-8")
    assert "ramping-arrival-rate" in js and "phase:hold" in js and "handleSummary" in js and "/work/config.json" in js
    assert "api:8000" not in js and "sandbox-pass" not in js


def test_the_hold_stage_is_always_preceded_by_a_ramp_so_it_is_flat():
    js = K6_SCRIPT.read_text(encoding="utf-8")
    assert "Math.max(C.ramp_s, 1)" in js and js.count("stages.push") == 3
    assert "if (C.ramp_s > 0)" not in js                                # the old conditional made hold glide 10 -> 20
    assert make_plan(ramp_s=0).total_s == 15 + 1 + 60


# --- reading the summary ------------------------------------------------------------

def test_hold_phase_numbers_become_a_measurement():
    raw = raw_result(hold={200: 5800, 500: 100, 0: 50, 404: 50}, p95=55.0, dropped=12)
    r = parse_result(raw, make_plan(), exit_code=0)
    assert (r.requests, r.duration_s, r.p95_ms, r.p50_ms, r.p99_ms) == (6000, 60, 55.0, 30.0, 150.0)
    assert r.achieved_rps == 100 and r.target_rps == 100
    assert r.status_counts == {200: 5800, 404: 50, 500: 100, 0: 50}
    assert r.error_rate == pytest.approx(150 / 6000)                  # 5xx and timeouts, not 404s
    assert r.timeouts == 50 and r.dropped_iterations == 12 and not r.aborted


def test_unlisted_statuses_are_counted_as_errors_in_an_other_bucket():
    r = parse_result(raw_result(hold={200: 90, "total": 100}), make_plan(), 0)
    assert r.status_counts == {200: 90, -1: 10} and r.error_rate == 0.1


def test_an_early_abort_is_measured_over_the_whole_run():
    raw = raw_result(hold=None, whole={200: 300, 500: 700}, failed_rate=0.7, whole_rate=50.0)
    r = parse_result(raw, make_plan(), exit_code=99)
    assert r.aborted and "70%" in r.abort_reason and r.requests == 1000
    assert r.duration_s == pytest.approx(20) and r.error_rate == 0.7


def test_exit_99_without_enough_failures_is_not_an_abort():
    r = parse_result(raw_result(hold={200: 6000}, failed_rate=0.01), make_plan(), exit_code=99)
    assert not r.aborted


def test_a_run_that_recorded_nothing_still_parses():
    r = parse_result({"metrics": {}}, make_plan(), 0)
    assert r.requests == 0 and r.achieved_rps == 0 and r.duration_s == 60


# --- running -------------------------------------------------------------------------

def test_run_load_writes_files_and_adds_machine_peaks_and_signals(m, tmp_path):
    sb = FakeSandbox(result=raw_result(hold={200: 6000}))
    r = run_load(m, sb, make_plan(), tmp_path / "run", FakeSampler())
    out = tmp_path / "run"
    assert json.loads((out / "config.json").read_text())["target_rps"] == 100
    assert (out / "script.js").read_text(encoding="utf-8") == K6_SCRIPT.read_text(encoding="utf-8")
    assert (r.loadgen_cpu_max_pct, r.host_cpu_max_pct) == (40, 55)
    assert r.signals["api_cpu"] == 90 and r.signals["db_connections"] == 17 and r.signals["achieved_request_rate"] == 100
    assert sb.last_timeout == 15 + 1 + 60 + 5 + 60                      # warmup + 1 s minimum ramp + hold + tail + start-up allowance


def test_a_failing_k6_is_a_runner_error_not_a_measurement(m, tmp_path):
    with pytest.raises(RunnerError, match="exit 107.*script exploded"):
        run_load(m, FakeSandbox(result=None, exit_code=107), make_plan(), tmp_path, FakeSampler())


def test_a_missing_summary_is_a_runner_error(m, tmp_path):
    with pytest.raises(RunnerError, match="no readable summary"):
        run_load(m, FakeSandbox(result=None), make_plan(), tmp_path, FakeSampler())


def test_the_sampler_is_stopped_even_when_k6_fails(m, tmp_path):
    stopped = []
    sampler = NS(start=lambda: None, stop=lambda: stopped.append(True) or Peaks())
    with pytest.raises(RunnerError):
        run_load(m, FakeSandbox(result=None, exit_code=1), make_plan(), tmp_path, sampler)
    assert stopped == [True]


# --- smoke probe ----------------------------------------------------------------------

def test_smoke_probe_logs_in_and_sends_the_token(m):
    sb = FakeSandbox()
    smoke_probe(m, sb, make_plan())
    login, call = sb.requests
    assert login[0] == "POST" and call[2]["headers"] == {"Authorization": "Bearer tok"}


def test_smoke_probe_catches_a_bad_login_and_a_bad_endpoint(m):
    with pytest.raises(RunnerError, match="login returned 401"):
        smoke_probe(m, FakeSandbox(login_status=401), make_plan())
    with pytest.raises(RunnerError, match="returned 401"):
        smoke_probe(m, FakeSandbox(endpoint_status=401), make_plan(auth_role="none"))


# --- a whole experiment ------------------------------------------------------------------

def experiment(m):
    cap = CapacityContext(rps=250, kind="exact", max_multiple=4.0)
    res = apply_guardrail([Card(**card(endpoint=ME, risk_id=None, load_multiple=1.0))], make_brief(), cap, m)
    return res.experiments[0]


def test_an_experiment_resets_probes_then_runs(m, tmp_path):
    sb = FakeSandbox(result=raw_result(hold={200: 15000}))
    r = run_experiment(m, sb, experiment(m), tmp_path, HEALTH, FakeSampler())
    assert sb.calls == ["reset", "k6"] and r.requests == 15000 and r.target_rps == 250
    assert [q[1] for q in sb.requests][-1].endswith("/api/me")


def test_a_failed_probe_means_no_load_is_ever_sent(m, tmp_path):
    sb = FakeSandbox(result=raw_result(hold={200: 1}), endpoint_status=401)
    with pytest.raises(RunnerError, match="smoke probe"):
        run_experiment(m, sb, experiment(m), tmp_path, HEALTH, FakeSampler())
    assert sb.calls == ["reset"]


def test_plan_from_experiment_carries_every_number(m):
    e = experiment(m)
    p = plan_from_experiment(e)
    assert (p.target_rps, p.warmup_s, p.hold_s, p.abort_error_rate, p.max_vus) == (250, 15, e.hold_s, e.abort_error_rate, 1000)


# --- machine sampling --------------------------------------------------------------------

def test_docker_stats_lines_are_parsed_and_junk_skipped():
    text = '{"Name":"chaos_target-api-1","CPUPerc":"180.5%"}\nnot json\n{"Name":"chaos_k6","CPUPerc":"12.0%"}'
    assert parse_docker_stats(text) == {"chaos_target-api-1": 180.5, "chaos_k6": 12.0}


def test_sampler_keeps_peaks_normalised_to_each_cpu_allowance(monkeypatch):
    sb = NS(api="api", db="db", container_name=lambda s: f"chaos_target-{s}-1", psql=lambda q: "17\n")
    s = Sampler(sb, "chaos_k6", {"api": 2.0, "db": 1.0, "loadgen": 1.0})
    readings = iter([
        '{"Name":"chaos_target-api-1","CPUPerc":"100%"}\n{"Name":"chaos_k6","CPUPerc":"60%"}',
        '{"Name":"chaos_target-api-1","CPUPerc":"190%"}\n{"Name":"chaos_target-db-1","CPUPerc":"45%"}',
        "",                                                         # a missed sample must not lower any peak
    ])
    monkeypatch.setattr(s, "_docker_stats", lambda: next(readings))
    monkeypatch.setattr("adapters.hostmetrics.psutil.cpu_percent", lambda interval=None: 70.0)
    for _ in range(3):
        s.sample_once()
    p = s.peaks
    assert (p.api_cpu_pct, p.db_cpu_pct, p.loadgen_cpu_pct, p.host_cpu_pct, p.db_connections) == (95.0, 45.0, 60.0, 70.0, 17.0)
