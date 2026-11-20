# tests/test_graph.py
"""The wired graph, with every outside call replaced by a script (no Docker, no model, no k6)."""
import pytest

import swarm.graph as G
from swarm.audit import AuditLog
from swarm.baseline import CapacityCheck
from swarm.calibration import BaselineRun
from swarm.card import Card
from swarm.graph import Deps, run_graph
from swarm.planner import PlannerError
from swarm.preflight import PreflightResult
from swarm.runner import RunnerError
from tests.helpers import CAP, MANIFEST, make_base, make_meas
from tests.test_tester import ME, card, make_brief


class Script:
    """Hands out scripted outcomes in order (an Exception instance is raised) and records its calls."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def run_with(monkeypatch, tmp_path, *, preflight=PreflightResult(True, details={"stack": "up"}),
             plan=None, check=CapacityCheck("ok", "fine", CAP), cards=(), runs=()):
    base = BaselineRun(make_base(), [], None, [], check)
    plan = plan if plan is not None else Script(make_brief())
    design = Script(*cards)
    run = Script(*runs)

    class FakeFacts:
        from_sandbox = staticmethod(lambda m, s: None)

    for name, fake in {
        "run_preflight": lambda m, s: preflight, "plan": plan, "Facts": FakeFacts,
        "CodeJail": lambda *a, **k: None, "baseline_target": lambda b, h: ("GET /api/me", None),
        "smoke_probe": lambda *a: None, "make_plan": lambda *a: None, "measure_baseline": lambda *a, **k: base,
        "save_baseline": lambda *a: None, "design_cards": design, "run_experiment": run,
    }.items():
        monkeypatch.setattr(G, name, fake)
    deps = Deps(MANIFEST, sandbox=None, client=None, audit=AuditLog(), out_dir=tmp_path, say=lambda _: None)
    return run_graph(deps), design, run


def fair(rps=20):
    return make_meas(rps)


def cards_of(*dicts):
    return [Card(**d) for d in dicts]


def test_a_clean_run_passes_and_is_reported(monkeypatch, tmp_path):
    rep, design, run = run_with(monkeypatch, tmp_path, cards=[cards_of(card(endpoint=ME, load_multiple=1.0))],
                                runs=[fair(250)])
    assert [r.outcome for r in rep.results] == ["pass"] and rep.label == "clean"
    assert len(design.calls) == 1 and len(run.calls) == 1
    assert (tmp_path / "report.md").exists()


def test_a_failed_preflight_stops_before_the_planner(monkeypatch, tmp_path):
    plan = Script()
    rep, _, _ = run_with(monkeypatch, tmp_path, preflight=PreflightResult(False, "health", "never became healthy"),
                         plan=plan)
    assert rep.label == "not_run" and plan.calls == []


def test_a_planner_failure_goes_straight_to_the_report(monkeypatch, tmp_path):
    rep, design, _ = run_with(monkeypatch, tmp_path, plan=Script(PlannerError("no brief")))
    assert rep.label == "not_run" and design.calls == []
    assert "planner failed: no brief" in rep.notes[0]


def test_unusable_capacity_stops_before_the_tester(monkeypatch, tmp_path):
    rep, design, _ = run_with(monkeypatch, tmp_path, check=CapacityCheck("unfit", "too noisy to trust"))
    assert rep.label == "not_run" and design.calls == []


def test_a_technical_flaw_is_rerun_without_going_back_to_the_tester(monkeypatch, tmp_path):
    saturated = make_meas(250, loadgen_cpu_max_pct=99.0)
    rep, design, run = run_with(monkeypatch, tmp_path, cards=[cards_of(card(endpoint=ME, load_multiple=1.0))],
                                runs=[saturated, fair(250)])
    assert [(r.outcome, r.attempts) for r in rep.results] == [("pass", 2)]
    assert len(design.calls) == 1 and len(run.calls) == 2


def test_persistent_technical_trouble_is_could_not_test(monkeypatch, tmp_path):
    rep, _, run = run_with(monkeypatch, tmp_path, cards=[cards_of(card(endpoint=ME, load_multiple=1.0))],
                           runs=[RunnerError("k6 crashed")] * 3)
    assert [(r.outcome, r.attempts) for r in rep.results] == [("could_not_test", 3)]
    assert len(run.calls) == MANIFEST.budgets.max_attempts and rep.label == "incomplete"


def test_a_design_flaw_goes_back_to_the_tester_with_the_reason(monkeypatch, tmp_path):
    first = card(endpoint=ME, load_multiple=1.0)
    refused = make_meas(250, requests=15000, status_counts={200: 7000, 401: 8000})
    rep, design, run = run_with(monkeypatch, tmp_path, cards=[cards_of(first), cards_of(first)],
                                runs=[refused, fair(250)])
    assert [(r.outcome, r.experiment.id) for r in rep.results] == [("pass", first["id"])]
    assert len(design.calls) == 2
    (_, kw) = design.calls[1]
    feedback = kw.get("feedback") or design.calls[1][0][-1]
    assert feedback and feedback[0].card.id == first["id"] and "client errors" in feedback[0].reason


def test_the_experiment_budget_holds_across_redesigns(monkeypatch, tmp_path):
    first = card(endpoint=ME, load_multiple=1.0)
    refused = make_meas(250, requests=15000, status_counts={200: 7000, 401: 8000})
    rep, design, run = run_with(monkeypatch, tmp_path, cards=[cards_of(first)] * 3, runs=[refused] * 3)
    assert [r.outcome for r in rep.results] == ["could_not_test"]
    assert len(design.calls) == MANIFEST.budgets.max_attempts       # one design plus the allowed revisions
    assert len(run.calls) == MANIFEST.budgets.max_attempts


@pytest.mark.parametrize("name", ["preflight", "planner", "baseline", "tester", "guardrail", "pick",
                                  "runner", "validator", "verdict", "report"])
def test_every_node_is_in_the_graph(monkeypatch, tmp_path, name):
    monkeypatch.setattr(G, "run_preflight", lambda m, s: PreflightResult(True))
    g = G.build_graph(Deps(MANIFEST, None, None, AuditLog(), tmp_path, lambda _: None)).get_graph()
    assert name in g.nodes
