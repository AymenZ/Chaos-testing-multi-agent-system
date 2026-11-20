# tests/test_validator.py
import pytest

from swarm.validator import validate
from tests.helpers import make_experiment, make_meas

LIMIT = 85.0


def check(**over):
    exp = make_experiment()
    return validate(exp, make_meas(exp.target_rps, **over), LIMIT)


def test_a_clean_run_is_valid_without_flags():
    v = check()
    assert v.valid and v.flaw is None and v.flags == []


@pytest.mark.parametrize("over,needle", [
    (dict(requests=0, status_counts={}), "no requests"),
    (dict(loadgen_cpu_max_pct=92.0), "load generator was saturated"),
    (dict(host_cpu_max_pct=95.0), "whole machine"),
    (dict(loadgen_cpu_max_pct=None), "metrics are missing"),
    (dict(host_cpu_max_pct=None), "metrics are missing"),
])
def test_technical_flaws_mean_rerun_the_same_card(over, needle):
    v = check(**over)
    assert not v.valid and v.flaw == "technical" and needle in " ".join(v.reasons)


def test_a_pile_of_refusals_is_a_design_flaw_not_a_fast_backend():
    v = check(status_counts={401: 9000, 200: 6000}, p95_ms=3.0)
    assert not v.valid and v.flaw == "design" and "401" in v.reasons[0] and "60%" in v.reasons[0]


def test_technical_problems_are_reported_before_design_ones():
    v = check(status_counts={401: 15000}, loadgen_cpu_max_pct=99.0)
    assert v.flaw == "technical"


def test_5xx_and_timeouts_are_backend_behaviour_so_the_test_stays_valid():
    assert check(status_counts={200: 9000, 500: 3000, 0: 3000}, error_rate=0.4).valid


def test_a_few_client_errors_are_tolerated():
    assert check(status_counts={200: 14000, 404: 1000}).valid          # ~7%, under the 10% line


def test_an_early_abort_for_server_errors_is_valid_and_flagged():
    v = check(aborted=True, abort_reason="error rate reached 60%", status_counts={200: 400, 500: 600},
              requests=1000, achieved_rps=40.0)
    assert v.valid and v.flags == ["aborted"]


def test_an_abort_caused_by_refusals_is_still_a_design_flaw():
    v = check(aborted=True, status_counts={401: 900, 200: 100}, requests=1000)
    assert v.flaw == "design"


def test_under_delivery_with_dropped_iterations_is_the_backend_being_slow():
    v = check(achieved_rps=100.0, dropped_iterations=3000)
    assert v.valid and v.flags[0].startswith("under_delivered")


def test_under_delivery_that_nothing_explains_is_a_technical_problem():
    v = check(achieved_rps=100.0, dropped_iterations=0)
    assert v.flaw == "technical" and "nothing explains it" in v.reasons[0]
