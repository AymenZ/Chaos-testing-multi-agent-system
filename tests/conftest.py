import pytest

from swarm.manifest import Manifest
from swarm.results import Baseline, HostStats, RunMetrics
from swarm.spec import ExpectedStimulus, MechanismSignal, Spec, Step


def make_spec(**over) -> Spec:
    base = dict(
        id="login-burst-01",
        hypothesis="POST /api/login p95 degrades at 30 concurrent users",
        steps=[Step(endpoint="/api/profile", auth="user", vus=30, duration_s=60)],
        expected_stimulus=ExpectedStimulus(min_achieved_vus=27, min_duration_s=55),
        mechanism_signals=[MechanismSignal(metric="db_active_connections", op=">=", value=15)],
    )
    base.update(over)
    return Spec(**base)


def make_metrics(**over) -> RunMetrics:
    base = dict(
        requests=10_000,
        achieved_vus=30,
        duration_s=60,
        status_counts={200: 10_000},
        p50_ms=40,
        p95_ms=100,
        p99_ms=150,
        error_rate=0.0,
        host=HostStats(cpu_peak_pct=50, loadgen_cpu_peak_pct=30, mem_free_mb_min=2000),
        signals={"db_active_connections": 15},
    )
    base.update(over)
    return RunMetrics(**base)


def make_baseline(p95s=(98, 100, 102)) -> Baseline:
    return Baseline.from_runs([make_metrics(p95_ms=p) for p in p95s])


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load("manifests/deployment_audit.yaml")
