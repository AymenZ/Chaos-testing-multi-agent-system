# tests/helpers.py
"""Builders shared by the validator, verdict and report tests."""
from swarm.baseline import BaselineSummary, CapacityContext
from swarm.card import Card
from swarm.guardrail import apply_guardrail
from swarm.manifest import load_manifest
from swarm.measurement import Measurement
from tests.test_tester import LOGIN, ME, card, make_brief

CAP = CapacityContext(rps=250, kind="exact", max_multiple=4.0)
MANIFEST = load_manifest("manifests/deployment_audit.yaml")
RULES = MANIFEST.rules.default           # p95 <= 2x baseline, errors <= 1%


def make_experiment(multiple=1.0, endpoint=ME, signals=("api_cpu",), **over):
    c = card(id=over.pop("id", "exp-one"), endpoint=endpoint, risk_id=None, load_multiple=multiple,
             signals=list(signals), **over)
    res = apply_guardrail([Card(**c)], make_brief(), CAP, MANIFEST)
    assert res.experiments, res.rejected
    return res.experiments[0]


def make_base(**over) -> BaselineSummary:
    d = dict(rps=20, p95_median_ms=20.0, p95_min_ms=19.0, p95_max_ms=22.0, error_rate_median=0.0,
             spread=0.1, noisy=False, low_samples=False)
    d.update(over)
    return BaselineSummary(**d)


def make_meas(target_rps=250, **over) -> Measurement:
    d = dict(target_rps=target_rps, duration_s=60, requests=int(target_rps * 60), achieved_rps=float(target_rps),
             p50_ms=15.0, p95_ms=25.0, p99_ms=40.0, error_rate=0.0, status_counts={200: int(target_rps * 60)},
             loadgen_cpu_max_pct=30.0, host_cpu_max_pct=50.0,
             signals={"api_cpu": 80.0, "db_cpu": 20.0, "db_connections": 4.0})
    d.update(over)
    return Measurement(**d)
