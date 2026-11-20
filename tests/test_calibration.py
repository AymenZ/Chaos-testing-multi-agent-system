# tests/test_calibration.py
"""Baseline + capacity measurement logic against a fake backend whose latency grows with load."""
import pytest

from swarm.calibration import CalibrationError, calibrate, load_baseline, measure_baseline, save_baseline
from swarm.manifest import load_manifest
from tests.helpers import make_meas
from tests.test_tester import make_brief

KNEE = 160          # the fake backend is healthy up to here, then degrades


class FakeBackend:
    def __init__(self, knee=KNEE, wobble=(19.0, 20.0, 21.0, 20.0, 22.0), broken_ramp_start=False, flaky=None):
        self.knee, self.wobble, self.broken_ramp_start, self.flaky = knee, wobble, broken_ramp_start, flaky
        self.resets, self.loads, self._base_runs = 0, [], 0

    def reset(self):
        self.resets += 1

    def load(self, plan, name):
        self.loads.append((name, plan.target_rps))
        if self.flaky and name.endswith(self.flaky["try"]) and plan.target_rps == self.flaky.get("rps", plan.target_rps):
            return make_meas(plan.target_rps, **self.flaky["over"])
        if "-baseline-" in name:
            p95 = self.wobble[self._base_runs % len(self.wobble)]
            self._base_runs += 1
        elif self.broken_ramp_start and plan.target_rps == 20:
            p95 = 900.0
        else:
            p95 = 20.0 if plan.target_rps <= self.knee else 20.0 * 6
        return make_meas(plan.target_rps, p95_ms=p95)


@pytest.fixture
def m():
    m = load_manifest("manifests/deployment_audit.yaml")
    m.baseline.repeats, m.baseline.ramp_step_factor = 5, 2.0     # pinned: the manifest's values are tuned for speed
    return m                                                    # so: normal 20 rps, x2 steps, 5 repeats, max 1000 rps


def run(m, backend, progress=lambda _: None):
    return measure_baseline(m, make_brief(), backend.load, backend.reset, progress)


def test_baseline_then_ramp_finds_the_knee_and_stops_at_the_first_unhealthy_step(m):
    b = FakeBackend()
    r = run(m, b)
    assert r.summary.rps == 20 and r.summary.p95_median_ms == 20.0 and len(r.runs) == 5
    assert (r.capacity.status, r.capacity.rps, r.capacity.first_bad_rps) == ("found", 160, 320)
    assert r.check.outcome == "ok" and r.check.context.max_multiple == 6.25
    ramp = [rps for name, rps in b.loads if "-ramp-" in name]
    assert ramp == [20, 40, 80, 160, 320]                          # 640 and 1000 were never run
    assert b.resets == 5 + 5                                       # the database is reset before every run


def test_the_target_comes_from_the_planners_baseline_choice(m):
    b = FakeBackend()
    run(m, b)
    assert all(name.startswith("get-api-me-") for name, _ in b.loads)     # GET /api/me is the brief's choice


def test_unhealthy_at_usual_traffic_is_a_finding(m):
    r = run(m, FakeBackend(broken_ramp_start=True))
    assert r.capacity.status == "below_start" and r.check.outcome == "cannot_sustain"


def test_a_technical_flaw_is_rerun_after_a_reset(m):
    b = FakeBackend(flaky={"try": "baseline-2-try1", "over": {"loadgen_cpu_max_pct": 97.0}})
    r = run(m, b)
    assert len(r.runs) == 5 and b.resets == 5 + 1 + 5              # one extra reset for the rerun
    names = [n for n, _ in b.loads]
    assert names.count("get-api-me-baseline-2-try1") == 1 and "get-api-me-baseline-2-try2" in names


def test_a_persistent_technical_flaw_stops_calibration(m):
    class AlwaysSaturated(FakeBackend):
        def load(self, plan, name):
            return make_meas(plan.target_rps, loadgen_cpu_max_pct=99.0)
    with pytest.raises(CalibrationError, match="after 3 attempts.*load generator was saturated"):
        run(m, AlwaysSaturated())


def test_a_design_flaw_is_not_retried(m):
    class Refused(FakeBackend):
        def load(self, plan, name):
            self.loads.append((name, plan.target_rps))
            return make_meas(plan.target_rps, status_counts={401: int(plan.target_rps * 60)})
    b = Refused()
    with pytest.raises(CalibrationError, match="baseline request itself is wrong"):
        run(m, b)
    assert len(b.loads) == 1


def test_a_brief_without_a_baseline_target_stops_early(m):
    brief = make_brief()
    brief.output.baseline_targets.clear()
    with pytest.raises(CalibrationError, match="no usable baseline target"):
        measure_baseline(m, brief, FakeBackend().load, lambda: None)


def test_progress_is_reported(m):
    seen = []
    run(m, FakeBackend(), seen.append)
    assert seen[0].startswith("baseline run 1/5") and any(s.startswith("ramp step at 320") for s in seen)


def test_saved_calibration_can_be_loaded_by_the_later_nodes(m, tmp_path):
    r = run(m, FakeBackend())
    save_baseline(r, tmp_path / "baseline.json")
    summary, check = load_baseline(tmp_path / "baseline.json")
    assert summary == r.summary and check == r.check and check.context.rps == 160


def test_the_health_check_is_never_the_baseline_target(m):
    from swarm.brief import EndpointNote, Recipe
    from swarm.calibration import baseline_target
    brief = make_brief()
    health = EndpointNote(key="GET /health", load_suitability="good", writes_data=False, cost_notes="static",
                          recipe=Recipe(auth_role="none"), evidence=[])
    brief.output.endpoints.insert(0, health)
    brief.output.baseline_targets[:] = ["GET /health", "GET /api/me"]
    assert baseline_target(brief, "/health")[0] == "GET /api/me"
    brief.output.baseline_targets[:] = ["GET /health"]
    with pytest.raises(CalibrationError, match="health check does not count"):
        baseline_target(brief, "/health")


def test_any_endpoint_can_be_measured_not_only_the_baseline_one(m):
    b = FakeBackend()
    r = measure_baseline(m, make_brief(), b.load, b.reset, key="POST /api/login")
    assert r.endpoint == "POST /api/login" and all(n.startswith("post-api-login-") for n, _ in b.loads)


def test_an_endpoint_without_a_recipe_cannot_be_measured(m):
    with pytest.raises(CalibrationError, match="no recipe"):
        measure_baseline(m, make_brief(), FakeBackend().load, lambda: None, key="GET /api/users/{user_id}")


def test_calibrate_takes_the_second_go_the_capacity_check_asks_for(m):
    class FewRequests(FakeBackend):                  # too few requests for a reliable p95: check asks to retry
        def load(self, plan, name):
            self.loads.append((name, plan.target_rps))
            return make_meas(plan.target_rps, requests=100)
    b = FewRequests()
    r = calibrate(m, make_brief(), b.load, b.reset)
    assert r.check.outcome == "unfit"                # asked once, measured again, still unusable: stop
    assert sum("-baseline-1-" in n for n, _ in b.loads) == 2


def test_a_fast_endpoint_is_not_called_unhealthy_under_the_p95_floor(m):
    class Fast(FakeBackend):                         # 5 ms normally, 35 ms at 160 rps and above
        def load(self, plan, name):
            self.loads.append((name, plan.target_rps))
            return make_meas(plan.target_rps, p95_ms=5.0 if plan.target_rps < 160 else 35.0)
    r = run(m, Fast(wobble=(5.0,)))
    assert r.capacity.status == "not_reached" and r.capacity.rps == 1000      # 35 ms is under the 50 ms floor
