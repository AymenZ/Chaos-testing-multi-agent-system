# tests/test_harness.py
"""The temporary routing used to try the nodes together, against scripted runs."""
from swarm.harness import run_one
from swarm.validator import Validity
from tests.helpers import MANIFEST, make_base, make_experiment, make_meas

EXP = make_experiment(1.0)
BASE = make_base()
OK = Validity(True)


def meas(p95=25.0, **over):
    return make_meas(EXP.target_rps, p95_ms=p95, **over)


def fair(p95=25.0):
    return meas(p95), [], OK


def unfair(flaw="technical", reason="load generator saturated"):
    return None, [reason], Validity(False, flaw, [reason])


def setup_error():
    return None, ["smoke probe: login returned 401"], None


class Script:
    """Hands out scripted outcomes in order and remembers the run names it was asked for."""

    def __init__(self, *outcomes):
        self.outcomes, self.names = list(outcomes), []

    def __call__(self, exp, name):
        self.names.append(name)
        return self.outcomes.pop(0)


def go(*outcomes):
    s = Script(*outcomes)
    return run_one(MANIFEST, EXP, BASE, s), s


def test_a_clean_run_passes_in_one_attempt():
    r, s = go(fair())
    assert (r.outcome, r.attempts, s.names) == ("pass", 1, ["exp-one-try1"])


def test_a_technical_flaw_is_rerun_and_can_then_pass():
    r, s = go(unfair(), fair())
    assert (r.outcome, r.attempts, s.names) == ("pass", 2, ["exp-one-try1", "exp-one-try2"])


def test_setup_errors_count_as_technical_and_are_retried():
    r, _ = go(setup_error(), fair())
    assert r.outcome == "pass" and r.attempts == 2


def test_persistent_trouble_is_could_not_test_never_a_pass():
    r, s = go(unfair(), setup_error(), unfair())
    assert r.outcome == "could_not_test" and r.attempts == 3 and len(s.names) == 3 and r.measurement is None


def test_a_design_flaw_is_not_retried():
    r, s = go(unfair("design", "70% of requests were refused"))
    assert r.outcome == "could_not_test" and len(s.names) == 1 and any("tester" in x for x in r.reasons)


def test_a_clear_fail_is_confirmed_by_one_repeat():
    r, s = go(fair(120.0), fair(130.0))
    assert r.outcome == "fail" and "confirmed on repeat" in r.reasons[-1] and s.names[-1] == "exp-one-confirm"


def test_a_fail_that_does_not_repeat_is_inconclusive():
    r, _ = go(fair(120.0), fair(25.0))
    assert r.outcome == "inconclusive" and "unstable" in r.reasons[0]


def test_an_unfair_confirmation_run_leaves_the_result_inconclusive():
    r, _ = go(fair(120.0), unfair())
    assert r.outcome == "inconclusive" and "confirmation run was not a fair test" in r.reasons[0]


def test_a_borderline_result_is_judged_on_the_median_of_three_runs():
    r, s = go(fair(44.0), fair(25.0), fair(30.0))               # limit is 40: borderline, but the median (30) is fine
    assert r.outcome == "pass" and s.names == ["exp-one-try1", "exp-one-median2", "exp-one-median3"]
    assert r.measurement.p95_ms == 30.0


def test_a_borderline_result_that_stays_bad_still_fails():
    r, _ = go(fair(44.0), fair(45.0), fair(100.0))
    assert r.outcome == "fail"


def test_progress_messages_are_emitted():
    seen = []
    run_one(MANIFEST, EXP, BASE, Script(fair()), seen.append)
    assert seen[0].startswith("exp-one: attempt 1") and seen[-1] == "exp-one: final pass"
