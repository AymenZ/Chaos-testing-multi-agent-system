# swarm/graph.py
"""The v1 graph. Nodes are thin: each calls the real work in its own module and returns state changes.

    preflight -> planner -> baseline -> tester -> guardrail -> pick -> runner -> validator -> verdict -> pick ...
                                                                ^                    |
                                                                +-- technical flaw: back to runner
                                                                +-- design flaw: feedback -> tester
    pick -> report when nothing is left (or a budget ran out). Any early failure goes straight to report.

Agents propose (planner, tester); everything that decides is plain code.
"""
import dataclasses
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from adapters.gemini import GeminiError
from swarm.audit import AuditLog
from swarm.baseline import check_capacity
from swarm.calibration import CalibrationError, baseline_target, make_plan, measure_baseline, save_baseline
from swarm.guardrail import apply_guardrail
from swarm.manifest import Manifest
from swarm.planner import Facts, PlannerError, plan
from swarm.preflight import PreflightError, PreflightResult, run_preflight
from swarm.report import Report, build_report, judged_result, untestable_result, write_report
from swarm.runner import RunnerError, run_experiment, run_load, smoke_probe
from swarm.state import SwarmState
from swarm.tester import Feedback, TesterError, design_cards
from swarm.tools.explorer import CodeJail
from swarm.validator import Validity, validate
from swarm.verdict import Confirm, Outcome, Verdict, judge, median_run, resolve_repeat

RECURSION_LIMIT = 500        # every experiment takes several hops, and retries add more


@dataclass
class Deps:
    """The live things the nodes need. Not state: it is fixed for the whole run."""
    m: Manifest
    sandbox: Any
    client: Any                              # Anthropic-shaped: client.messages.create(...)
    audit: AuditLog
    out_dir: Path
    say: Callable[[str], None] = print


def build_graph(deps: Deps):
    m, say = deps.m, deps.say
    health = str(m.target.base_url).rstrip("/") + m.target.health_path
    max_attempts = m.budgets.max_attempts

    def llm_calls_used() -> int:
        return sum(1 for e in deps.audit.events if e["kind"] == "model_call")

    def run_and_validate(exp, name: str) -> tuple[Any, Validity]:
        """One run through the validator. The measurement is None unless it was a fair test."""
        try:
            meas = run_experiment(m, deps.sandbox, exp, deps.out_dir / name, health)
        except RunnerError as e:
            return None, Validity(False, "technical", [str(e)])
        v = validate(exp, meas, m.resources.host_cpu_limit_pct)
        return (meas if v.valid else None), v

    # --- nodes -------------------------------------------------------------------------

    def preflight(state: SwarmState) -> dict:
        say("preflight: starting the sandbox")
        try:
            result = run_preflight(m, deps.sandbox)
        except PreflightError as e:
            result = PreflightResult(False, "preflight", str(e))
        return {"preflight": result, "started_at": time.monotonic()}

    def planner(state: SwarmState) -> dict:
        say("planner: studying the backend")
        try:
            brief = plan(m, deps.client, Facts.from_sandbox(m, deps.sandbox),
                         CodeJail(m.target.code_path, deps.audit), deps.audit)
        except (PlannerError, GeminiError) as e:
            return {"error": f"planner failed: {e}"}
        (deps.out_dir / "brief.json").parent.mkdir(parents=True, exist_ok=True)
        (deps.out_dir / "brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
        return {"brief": brief}

    def baseline(state: SwarmState) -> dict:
        brief = state["brief"]

        def measure():
            return measure_baseline(
                m, brief, lambda p, name: run_load(m, deps.sandbox, p, deps.out_dir / "baseline" / name),
                lambda: deps.sandbox.reset(health), progress=say)

        try:
            key, note = baseline_target(brief, m.target.health_path)
            smoke_probe(m, deps.sandbox, make_plan(m, key, note, m.baseline.normal_rps))
            say(f"baseline: measuring {key}")
            run = measure()
            if run.check.outcome == "retry":
                say(f"baseline: the capacity check asked for another go ({run.check.reason})")
                run = measure()
                run = dataclasses.replace(run, check=check_capacity(
                    run.summary, run.capacity, m.limits, m.baseline.normal_rps, retried=True))
        except (CalibrationError, RunnerError) as e:
            return {"error": f"baseline failed: {e}"}
        save_baseline(run, deps.out_dir / "baseline.json")
        say(f"baseline: capacity check says {run.check.outcome.upper()}: {run.check.reason}")
        return {"baseline": run}

    def tester(state: SwarmState) -> dict:
        feedback = state.get("feedback") or []
        say(f"tester: {'revising ' + str(len(feedback)) + ' card(s)' if feedback else 'designing experiment cards'}")
        try:
            cards = design_cards(m, deps.client, state["brief"], state["baseline"].check.context,
                                 deps.audit, feedback or None)
        except (TesterError, GeminiError) as e:
            if not feedback:
                return {"error": f"tester failed: {e}"}
            known = state["experiments"]
            return {"new_cards": [], "feedback": [], "results": [
                untestable_result(known[f.card.id], [f"the tester could not revise the card: {e}", f.reason],
                                  max_attempts) for f in feedback]}
        return {"new_cards": cards, "cards": {**state.get("cards", {}), **{c.id: c for c in cards}}}

    def guardrail(state: SwarmState) -> dict:
        feedback, known = state.get("feedback") or [], state.get("experiments", {})
        planned = state.get("planned", 0) - len(feedback)  # revised cards replace the ones they came from
        gr = apply_guardrail(state.get("new_cards", []), state["brief"], state["baseline"].check.context, m,
                             already_planned=planned)
        revised = {f.card.id for f in feedback}
        lost = [untestable_result(known[r.card_id], [f"the revised card was rejected: {r.reason}"], max_attempts)
                for r in gr.rejected if r.card_id in revised]
        for e in gr.experiments:
            say(f"guardrail: {e.id} {e.endpoint} at {e.load_multiple:g}x ({e.target_rps:.0f} rps)"
                + (f" [{'; '.join(e.adjustments)}]" if e.adjustments else ""))
        for r in gr.rejected:
            say(f"guardrail: rejected {r.card_id}: {r.reason}")
        return {"experiments": {**known, **{e.id: e for e in gr.experiments}},
                "queue": [*state.get("queue", []), *gr.experiments],
                "planned": planned + len(gr.experiments),
                "rejected": [r for r in gr.rejected if r.card_id not in revised],
                "results": lost, "feedback": [], "new_cards": []}

    def pick(state: SwarmState) -> dict:
        queue, feedback = list(state.get("queue", [])), list(state.get("feedback", []))
        known = state.get("experiments", {})
        over_time = time.monotonic() - state["started_at"] > m.budgets.wall_clock_s
        out_of_calls = llm_calls_used() + m.llm.tester_max_calls > m.budgets.max_llm_calls
        if over_time and (queue or feedback):
            say("pick: the wall-clock budget is used up; stopping")
            return {"truncated": True, "queue": [], "current": None, "feedback": [], "results": [
                untestable_result(known[f.card.id], ["the wall-clock budget ran out before the card could be revised"],
                                  state.get("attempt", 1)) for f in feedback]}
        if queue:
            nxt = queue.pop(0)
            return {"queue": queue, "current": nxt, "attempt": 0, "measurement": None, "validity": None, "reasons": []}
        if feedback and out_of_calls:
            say("pick: no model calls left to revise the remaining cards")
            return {"current": None, "feedback": [], "truncated": True, "results": [
                untestable_result(known[f.card.id], ["the model-call budget ran out before the card could be revised",
                                                     f.reason], state.get("attempt", 1)) for f in feedback]}
        return {"current": None}

    def runner(state: SwarmState) -> dict:
        exp, attempt = state["current"], state["attempt"] + 1
        say(f"runner: {exp.id} attempt {attempt}: {exp.endpoint} at {exp.load_multiple:g}x "
            f"({exp.target_rps:.0f} rps), {exp.total_s}s")
        try:
            meas = run_experiment(m, deps.sandbox, exp, deps.out_dir / f"{exp.id}-try{attempt}", health)
        except RunnerError as e:
            return {"attempt": attempt, "measurement": None, "reasons": [str(e)]}
        return {"attempt": attempt, "measurement": meas, "reasons": []}

    def validator(state: SwarmState) -> dict:
        exp, meas, attempt = state["current"], state.get("measurement"), state["attempt"]
        v = (Validity(False, "technical", state.get("reasons") or ["the run could not be carried out"])
             if meas is None else validate(exp, meas, m.resources.host_cpu_limit_pct))
        if v.valid:
            return {"validity": v}
        say(f"validator: {exp.id} was not a fair test ({v.flaw}): {'; '.join(v.reasons)}")
        if v.flaw == "design":
            used = state.get("redesigns", {}).get(exp.id, 0)
            if used < max_attempts - 1:
                card = state["cards"][exp.id]
                return {"validity": v, "redesigns": {**state.get("redesigns", {}), exp.id: used + 1},
                        "feedback": [*state.get("feedback", []), Feedback(card, "; ".join(v.reasons))]}
        elif attempt < max_attempts:
            return {"validity": v}                       # the router sends it back to the runner
        return {"validity": v, "results": [untestable_result(exp, v.reasons, attempt)]}

    def verdict(state: SwarmState) -> dict:
        exp, meas, v, attempt = state["current"], state["measurement"], state["validity"], state["attempt"]
        base, rules = state["baseline"].summary, m.rules.default
        outcome = judge(exp, meas, base, rules, v.flags)
        say(f"verdict: {exp.id}: {outcome.outcome.value}"
            + (f", confirming ({outcome.confirm.value})" if outcome.confirm is not Confirm.NONE else ""))
        if outcome.confirm is Confirm.REPEAT:
            again, again_v = run_and_validate(exp, f"{exp.id}-confirm")
            if again is not None:
                outcome = resolve_repeat(outcome, judge(exp, again, base, rules, again_v.flags))
            else:
                outcome = Verdict(outcome=Outcome.INCONCLUSIVE,
                                  reasons=["the confirmation run was not a fair test", *again_v.reasons, *outcome.reasons])
        elif outcome.confirm is Confirm.MEDIAN3:
            extra = [run_and_validate(exp, f"{exp.id}-median{i}")[0] for i in (2, 3)]
            meas = median_run([meas] + [r for r in extra if r is not None])
            outcome = judge(exp, meas, base, rules, v.flags)
        say(f"verdict: {exp.id}: final {outcome.outcome.value}")
        return {"results": [judged_result(exp, meas, outcome, v.flags, attempt)]}

    def report(state: SwarmState) -> dict:
        run = state.get("baseline")
        rep = build_report(preflight=state.get("preflight"), brief=state.get("brief"),
                           baseline=run.summary if run else None, capacity=run.check if run else None,
                           results=state.get("results", []), rejected=state.get("rejected", []),
                           truncated=state.get("truncated", False))
        if state.get("error"):
            rep.notes.insert(0, f"The run stopped early: {state['error']}")
        md, _ = write_report(rep, deps.out_dir)
        say(f"report: {rep.label.upper()}: {rep.headline}\n        {md}")
        return {"report": rep}

    # --- edges -------------------------------------------------------------------------

    def after_preflight(state: SwarmState) -> str:
        return "planner" if state["preflight"].ok else "report"

    def after_planner(state: SwarmState) -> str:
        return "report" if state.get("error") else "baseline"

    def after_baseline(state: SwarmState) -> str:
        if state.get("error") or state["baseline"].check.context is None:
            return "report"                               # unfit or cannot_sustain: the report says why
        return "tester"

    def after_tester(state: SwarmState) -> str:
        return "report" if state.get("error") else "guardrail"

    def after_pick(state: SwarmState) -> str:
        if state.get("truncated"):
            return "report"
        if state.get("current") is not None:
            return "runner"
        return "tester" if state.get("feedback") else "report"

    def after_validator(state: SwarmState) -> str:
        v = state["validity"]
        if v.valid:
            return "verdict"
        if v.flaw == "technical" and state["attempt"] < max_attempts:
            return "runner"
        return "pick"                                     # recorded as feedback or as could-not-test

    g = StateGraph(SwarmState)
    for name, fn in [("preflight", preflight), ("planner", planner), ("baseline", baseline), ("tester", tester),
                     ("guardrail", guardrail), ("pick", pick), ("runner", runner), ("validator", validator), ("verdict", verdict),
                     ("report", report)]:
        g.add_node(name, fn)
    g.add_edge(START, "preflight")
    g.add_conditional_edges("preflight", after_preflight, ["planner", "report"])
    g.add_conditional_edges("planner", after_planner, ["baseline", "report"])
    g.add_conditional_edges("baseline", after_baseline, ["tester", "report"])
    g.add_conditional_edges("tester", after_tester, ["guardrail", "report"])
    g.add_edge("guardrail", "pick")
    g.add_conditional_edges("pick", after_pick, ["runner", "tester", "report"])
    g.add_edge("runner", "validator")
    g.add_conditional_edges("validator", after_validator, ["verdict", "runner", "pick"])
    g.add_edge("verdict", "pick")
    g.add_edge("report", END)
    return g.compile()


def run_graph(deps: Deps) -> Report:
    final = build_graph(deps).invoke({}, {"recursion_limit": RECURSION_LIMIT})
    return final["report"]
