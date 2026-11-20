# tests/test_verdict.py
from swarm.verdict import Confirm, Outcome, judge, median_run, observed_signals, resolve_repeat
from tests.helpers import RULES, make_base, make_experiment, make_meas

NO_FLOOR = RULES.model_copy(update={"p95_floor_ms": 0})     # most tests here are about the ratio logic alone


def run(multiple=1.0, signals=("api_cpu",), base=None, flags=None, rules=NO_FLOOR, **over):
    exp = make_experiment(multiple, signals=signals)
    return judge(exp, make_meas(exp.target_rps, **over), base or make_base(), rules, flags)


def test_a_p95_under_the_floor_never_fails_whatever_the_ratio():
    v = run(base=make_base(p95_median_ms=5.0, p95_min_ms=4.0, p95_max_ms=6.0), p95_ms=35.0, rules=RULES)
    assert v.outcome is not Outcome.FAIL                       # 7x slower, but 35 ms is under the 50 ms floor


def test_above_the_floor_the_ratio_still_decides():
    v = run(base=make_base(p95_median_ms=5.0, p95_min_ms=4.0, p95_max_ms=6.0), p95_ms=80.0, rules=RULES)
    assert v.outcome is Outcome.FAIL


def test_clean_numbers_and_observed_signals_pass():
    v = run()
    assert v.outcome is Outcome.PASS and v.confirm is Confirm.NONE and v.observed == ["api_cpu"]


def test_clean_numbers_without_the_expected_signal_are_a_weak_pass_never_a_pass():
    v = run(signals=("db_connections",))                       # 4 connections: the pool never filled
    assert v.outcome is Outcome.INCONCLUSIVE and v.weak_pass and v.missing == ["db_connections"]


def test_a_clear_p95_regression_fails_and_asks_for_a_repeat():
    v = run(p95_ms=120.0)
    assert v.outcome is Outcome.FAIL and v.confirm is Confirm.REPEAT and "120 ms" in v.reasons[0]


def test_slower_but_inside_the_baselines_usual_range_is_inconclusive_not_a_fail():
    v = run(base=make_base(p95_max_ms=60.0, noisy=True), p95_ms=45.0)       # limit is 40, but baseline reached 60
    assert v.outcome is Outcome.INCONCLUSIVE and not v.weak_pass and "usual range" in v.reasons[0]


def test_a_fail_close_to_the_threshold_asks_for_three_runs():
    v = run(p95_ms=44.0)                                        # limit 40, within 20%
    assert v.outcome is Outcome.FAIL and v.confirm is Confirm.MEDIAN3


def test_error_rate_is_judged_relative_to_the_baseline():
    assert run(base=make_base(error_rate_median=0.005), error_rate=0.014).outcome is Outcome.PASS    # limit 0.015
    assert run(base=make_base(error_rate_median=0.005), error_rate=0.03).outcome is Outcome.FAIL


def test_an_aborted_run_is_a_fail():
    v = run(aborted=True, abort_reason="error rate reached 60% (limit 50%)", error_rate=0.6)
    assert v.outcome is Outcome.FAIL and "60%" in v.reasons[0]


# --- above measured capacity ---------------------------------------------------------------

def test_above_capacity_p95_is_reported_but_not_judged():
    v = run(multiple=1.5, p95_ms=800.0)                         # would fail the 2x rule at or below capacity
    assert v.outcome is Outcome.PASS and any("not judged above measured capacity" in r for r in v.reasons)


def test_above_capacity_errors_still_fail():
    assert run(multiple=1.5, error_rate=0.08).outcome is Outcome.FAIL


# --- under-delivery ---------------------------------------------------------------------------

def test_a_clean_result_at_a_load_that_was_not_delivered_says_little():
    v = run(flags=["under_delivered:40%"])
    assert v.outcome is Outcome.INCONCLUSIVE and not v.weak_pass and "not delivered" in v.reasons[-1]


# --- signals ------------------------------------------------------------------------------------

def test_each_catalog_signal_has_an_observed_threshold():
    exp = make_experiment(signals=("request_latency_p95", "error_rate", "achieved_request_rate",
                                   "api_cpu", "db_cpu", "db_connections"))
    quiet = make_meas(exp.target_rps)
    seen, missing = observed_signals(exp, quiet, make_base())
    assert seen == ["api_cpu"] and len(missing) == 5
    loud = make_meas(exp.target_rps, p95_ms=35.0, error_rate=0.002, achieved_rps=200.0,
                     signals={"api_cpu": 75.0, "db_cpu": 71.0, "db_connections": 12.0})
    assert observed_signals(exp, loud, make_base())[1] == []


# --- repeats --------------------------------------------------------------------------------------

def test_repeat_resolution():
    fail, ok = run(p95_ms=120.0), run()
    assert resolve_repeat(fail, fail).outcome is Outcome.FAIL and "confirmed" in resolve_repeat(fail, fail).reasons[-1]
    assert resolve_repeat(fail, ok).outcome is Outcome.INCONCLUSIVE


def test_the_median_run_is_the_one_with_the_middle_p95():
    runs = [make_meas(p95_ms=p) for p in (300.0, 100.0, 200.0)]
    assert median_run(runs).p95_ms == 200.0
