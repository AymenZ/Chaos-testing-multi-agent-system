# tests/test_report.py
import json

from swarm.baseline import CapacityCheck, CapacityContext
from swarm.guardrail import Rejection
from swarm.preflight import PreflightResult
from swarm.report import build_report, judged_result, render_markdown, untestable_result, write_report
from swarm.verdict import judge
from tests.helpers import RULES, make_base, make_experiment, make_meas
from tests.test_tester import BYID, LOGIN, ME, make_brief

BASE = make_base()
OK_ENV = PreflightResult(ok=True, details={"isolated": True, "users": 50000, "login": "ok"})
CAP_OK = CapacityCheck("ok", "capacity found at 250 rps", CapacityContext(250, "exact", 4.0))


def judged(multiple=1.0, endpoint=ME, id="exp-one", **over):
    exp = make_experiment(multiple, endpoint=endpoint, id=id)
    meas = make_meas(exp.target_rps, **over)
    return judged_result(exp, meas, judge(exp, meas, BASE, RULES))


def report(results, **kw):
    kw.setdefault("preflight", OK_ENV); kw.setdefault("baseline", BASE)
    kw.setdefault("capacity", CAP_OK); kw.setdefault("brief", make_brief())
    return build_report(results=results, **kw)


def test_everything_passing_with_signals_is_clean():
    r = report([judged(), judged(endpoint=LOGIN, id="login-one")])
    assert r.label == "clean" and "2 passed" in r.headline and r.findings == []


def test_a_confirmed_failure_makes_the_label_findings_and_names_the_load():
    r = report([judged(), judged(endpoint=LOGIN, id="login-fail", p95_ms=300.0)])
    assert r.label == "findings" and len(r.findings) == 1
    assert "login-fail" in r.findings[0] and "POST /api/login" in r.findings[0] and "1x its capacity" in r.findings[0]


def test_untestable_and_inconclusive_results_make_it_incomplete_never_clean():
    cant = untestable_result(make_experiment(id="cant-run"), ["smoke probe: login returned 401"], attempts=3)
    assert cant.outcome == "could_not_test"
    assert report([judged(), cant]).label == "incomplete"
    exp = make_experiment(id="weak-one", signals=("db_connections",))     # the pool never fills: signal missing
    weak = judged_result(exp, make_meas(exp.target_rps), judge(exp, make_meas(exp.target_rps), BASE, RULES))
    r = report([weak])
    assert weak.weak_pass and r.label == "incomplete" and any("weak pass" in n for n in r.notes)


def test_a_rejected_card_or_a_truncated_run_means_incomplete():
    assert report([judged()], rejected=[Rejection("card-x", "endpoint is not runnable")]).label == "incomplete"
    r = report([judged()], truncated=True)
    assert r.label == "incomplete" and any("cut short" in n for n in r.notes)


def test_a_failed_environment_means_not_run():
    r = build_report(preflight=PreflightResult(False, "login", "login returned 401"))
    assert r.label == "not_run" and "login" in r.headline and r.results == []


def test_cannot_sustain_usual_traffic_is_reported_as_a_finding():
    cap = CapacityCheck("cannot_sustain", "even usual traffic (20 rps) was unhealthy: the backend cannot sustain its normal load")
    r = report([], capacity=cap)
    assert r.label == "findings" and r.findings[0].startswith("even usual traffic")


def test_an_unusable_capacity_means_not_run():
    r = report([], capacity=CapacityCheck("unfit", "limits leave no headroom"))
    assert r.label == "not_run" and "headroom" in r.headline
    assert report([]).label == "not_run"


def test_what_was_not_covered_is_spelled_out():
    r = report([judged(endpoint=ME)], rejected=[Rejection("card-x", "over budget")])
    text = " | ".join(r.not_covered)
    assert f"{BYID}: not tested (needs_data)" in text and "GET /api/admin/logs: not tested (avoid)" in text
    assert f"{LOGIN}: runnable but no experiment targeted it" in text
    assert "no rate limiting information found" in text and "card-x was rejected" in text


def test_aborted_runs_and_a_noisy_baseline_are_noted():
    exp = make_experiment(id="aborted-one")
    meas = make_meas(exp.target_rps, aborted=True, abort_reason="error rate reached 60%", error_rate=0.6)
    res = judged_result(exp, meas, judge(exp, meas, BASE, RULES, ["aborted"]), flags=["aborted"])
    r = report([res], baseline=make_base(noisy=True))
    assert any("stopped early" in n for n in r.notes) and any("noisy" in n for n in r.notes)


def test_the_markdown_has_a_table_the_label_and_the_adjustments():
    adjusted = make_experiment(9.0, id="big-ask")                       # clamped to 4x by the guardrail
    meas = make_meas(adjusted.target_rps, p95_ms=900.0, error_rate=0.02)
    res = judged_result(adjusted, meas, judge(adjusted, meas, BASE, RULES))
    md = render_markdown(report([judged(), res]))
    assert "**Result: FINDINGS**" in md and "| big-ask | GET /api/me | 4x its capacity (1000 rps) | fail |" in md
    assert "900 ms (45.0x)" in md and "adjusted: load: 9.0x -> 4.0x" in md
    assert "## What was not covered" in md and "Clean means only" in md


def test_write_report_saves_markdown_and_json(tmp_path):
    md, js = write_report(report([judged()]), tmp_path / "out")
    assert md.read_text(encoding="utf-8").startswith("# Resilience test report")
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["label"] == "clean" and data["results"][0]["experiment"]["id"] == "exp-one"
