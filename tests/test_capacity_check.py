# tests/test_capacity_check.py
"""The gate between Baseline and the tester: is the capacity number exact, a lower bound, or unusable?"""
from swarm.baseline import BaselineSummary, Capacity, check_capacity, ramp_rates
from swarm.manifest import Limits

LIMITS = Limits(max_rps=1000, max_duration_s=180, max_vus=1000, request_timeout_s=5)
NORMAL = 20


def base(**over) -> BaselineSummary:
    d = dict(rps=NORMAL, p95_median_ms=20, p95_min_ms=19, p95_max_ms=22, error_rate_median=0.0,
             spread=0.1, noisy=False, low_samples=False)
    d.update(over)
    return BaselineSummary(**d)


def check(cap, b=None, retried=False):
    return check_capacity(b or base(), cap, LIMITS, NORMAL, retried=retried)


def test_found_capacity_is_exact_and_reports_headroom():
    r = check(Capacity("found", 250, 300))
    assert r.outcome == "ok" and r.context.kind == "exact"
    assert (r.context.rps, r.context.max_multiple) == (250, 4.0)


def test_unhealthy_at_usual_traffic_is_a_finding_not_a_retry():
    cap = Capacity("below_start", None, NORMAL)
    for retried in (False, True):                                    # retrying lower would hide the finding
        r = check(cap, retried=retried)
        assert r.outcome == "cannot_sustain" and "usual traffic (20 rps)" in r.reason and r.context is None


def test_a_ramp_that_reaches_the_ceiling_leaves_nothing_to_stress_with():
    r = check(Capacity("not_reached", 960, None))
    assert r.outcome == "unfit" and "headroom" in r.reason


def test_all_steps_healthy_after_a_retry_is_a_usable_lower_bound():
    r = check(Capacity("not_reached", 400, None), retried=True)
    assert r.outcome == "lower_bound" and r.context.kind == "lower_bound" and r.context.max_multiple == 2.5


def test_all_steps_healthy_but_ramp_ended_below_the_limit_means_the_ramp_was_too_short():
    r = check(Capacity("not_reached", 400, None))
    assert r.outcome == "retry" and "too short" in r.reason


def test_capacity_close_to_max_rps_leaves_no_room_to_stress():
    r = check(Capacity("found", 900, 950))                           # 1000 / 900 = 1.11 < 1.2
    assert r.outcome == "unfit" and "raise the limit" in r.reason


def test_too_few_samples_blocks_even_a_found_capacity():
    assert check(Capacity("found", 250, 300), base(low_samples=True)).outcome == "retry"
    assert check(Capacity("found", 250, 300), base(low_samples=True), retried=True).outcome == "unfit"


def test_noisy_baseline_is_flagged_but_does_not_block():
    r = check(Capacity("found", 250, 300), base(noisy=True))
    assert r.outcome == "ok" and r.noisy


# --- the ramp's rates ------------------------------------------------------------------

def test_ramp_starts_at_usual_traffic_grows_by_the_factor_and_ends_at_the_limit():
    assert ramp_rates(20, 2.0, 1000) == [20, 40, 80, 160, 320, 640, 1000]


def test_a_step_just_below_the_limit_is_replaced_by_the_limit():
    assert ramp_rates(20, 2.0, 650) == [20, 40, 80, 160, 320, 650]   # 640 is within 5% of 650


def test_usual_traffic_equal_to_the_limit_is_a_single_step():
    assert ramp_rates(500, 2.0, 500) == [500]
