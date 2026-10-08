import pytest
from swarm.baseline import find_knee, summarize_baseline, clearly_slower
from swarm.manifest import RuleSet
from swarm.measurement import Measurement

RULES = RuleSet(p95_vs_baseline_max=2.0, error_rate_max=0.01, recovery_max_s=30)

def m(p95, target=100, achieved=None, err=0.0, requests=1000):
    return Measurement(
        target_rps=target, duration_s=60, requests=requests,
        achieved_rps=target if achieved is None else achieved,
        p50_ms=p95 / 2, p95_ms=p95, p99_ms=p95 * 1.5, error_rate=err,
    )

def test_stable_baseline_is_not_noisy():
    b = summarize_baseline([m(20), m(21), m(19), m(20), m(22)])
    assert b.p95_median_ms == 20 and not b.noisy

def test_wobbly_baseline_is_flagged_noisy():
    assert summarize_baseline([m(10), m(25), m(14), m(30), m(12)]).noisy

def test_too_few_runs_rejected():
    with pytest.raises(ValueError):
        summarize_baseline([m(20), m(21)])

def test_wobble_is_not_clearly_slower():
    b = summarize_baseline([m(20), m(24), m(18), m(22), m(21)])
    assert not clearly_slower(25, b, 2.0)    # above max, but not 2x median
    assert clearly_slower(50, b, 2.0)

def test_knee_found():
    b = summarize_baseline([m(20)] * 3)
    steps = [m(21, 50), m(22, 100), m(30, 200), m(80, 400), m(500, 800, achieved=300, err=0.2)]
    c = find_knee(steps, b, RULES)
    assert (c.status, c.rps, c.first_bad_rps) == ("found", 200, 400)

def test_ramp_started_too_high():
    b = summarize_baseline([m(20)] * 3)
    assert find_knee([m(90, 50)], b, RULES).status == "below_start"

def test_ramp_ended_too_low():
    b = summarize_baseline([m(20)] * 3)
    assert find_knee([m(21, 50), m(22, 100)], b, RULES).status == "not_reached"