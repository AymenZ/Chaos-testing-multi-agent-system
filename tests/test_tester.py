# tests/test_tester.py
"""The request tester, driven by a scripted fake model: no network, no API key."""
import json
from types import SimpleNamespace as NS

import pytest

from swarm.audit import AuditLog
from swarm.baseline import CapacityContext
from swarm.brief import Brief, Evidence, EndpointNote, ManifestContext, PlannerOutput, Recipe, Risk
from swarm.manifest import load_manifest
from swarm.tester import Feedback, check_cards, design_cards, runnable_endpoints
from swarm.tester import TesterError as DesignFailed
from swarm.card import Card

CAP = CapacityContext(rps=250, kind="exact", max_multiple=4.0)
ME, LOGIN, BYID, ADMIN_LOGS = "GET /api/me", "POST /api/login", "GET /api/users/{user_id}", "GET /api/admin/logs"


def make_brief() -> Brief:
    ev = [Evidence(ref="app/security.py", quote="pwd_context = CryptContext")]

    def note(key, suit, recipe):
        return EndpointNote(key=key, load_suitability=suit, writes_data=False, cost_notes=f"cost of {key}",
                            recipe=recipe, evidence=ev)
    out = PlannerOutput(
        summary="A small login service backed by one Postgres table.",
        endpoints=[note(ME, "good", Recipe(auth_role="user")),
                   note(LOGIN, "caution", Recipe(auth_role="none", form_body={"username": "{{user.email}}", "password": "{{user.password}}"})),
                   note(BYID, "needs_data", None),
                   note(ADMIN_LOGS, "avoid", Recipe(auth_role="admin"))],
        baseline_targets=[ME],
        risks=[Risk(id="hash-cost", statement="password verification is CPU heavy on every login", endpoints=[LOGIN],
                    signals=["api_cpu", "request_latency_p95"], confidence="medium", evidence=ev)],
        unknowns=["no rate limiting information found"])
    m = load_manifest("manifests/deployment_audit.yaml")
    return Brief(output=out, context=ManifestContext.from_manifest(m))


def card(id="login-overload", endpoint=LOGIN, **over) -> dict:
    d = dict(id=id, hypothesis="login p95 degrades when pushed past measured capacity", endpoint=endpoint,
             risk_id="hash-cost", load_multiple=1.5, hold_s=60, ramp_s=10, min_delivered=0.9,
             signals=["api_cpu"], abort_error_rate=0.5)
    d.update(over)
    return d


def reply(text: str, stop="end_turn"):
    return NS(content=[NS(type="text", text=text)], stop_reason=stop, usage=NS(input_tokens=500, output_tokens=120))


def cards_reply(*cards) -> NS:
    return reply(json.dumps({"cards": list(cards)}))


class ScriptedClient:
    def __init__(self, *script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        assert self.script, "more model calls than the script has"
        return self.script.pop(0)


@pytest.fixture
def env():
    return NS(m=load_manifest("manifests/deployment_audit.yaml"), brief=make_brief(), audit=AuditLog())


def run(env, *script, feedback=None):
    client = ScriptedClient(*script)
    return design_cards(env.m, client, env.brief, CAP, env.audit, feedback), client


def test_valid_cards_are_returned(env):
    cards, client = run(env, cards_reply(card(), card(id="me-baseline", endpoint=ME, risk_id=None, load_multiple=0.5, signals=["error_rate"])))
    assert [c.id for c in cards] == ["login-overload", "me-baseline"] and cards[0].load_multiple == 1.5
    assert env.audit.kinds() == ["model_call", "cards_accepted"]


def test_request_uses_manifest_settings_and_a_json_schema(env):
    _, client = run(env, cards_reply(card()))
    call = client.calls[0]
    assert call["model"] == env.m.llm.tester_model and call["output_config"]["effort"] == env.m.llm.effort
    assert call["output_config"]["format"]["type"] == "json_schema" and "tools" not in call
    assert "api_cpu" in call["system"] and "tool_choice" not in call


def test_the_model_only_sees_runnable_endpoints_and_no_recipes(env):
    _, client = run(env, cards_reply(card()))
    sent = client.calls[0]["messages"][0]["content"]
    assert ME in sent and LOGIN in sent and "max_multiple" in sent and '"rps": 250' in sent
    assert BYID not in sent and ADMIN_LOGS not in sent                   # needs_data and avoid are hidden
    assert "form_body" not in sent and "{{user.password}}" not in sent   # the recipe is attached by code later


def test_runnable_filter(env):
    assert set(runnable_endpoints(env.brief)) == {ME, LOGIN}


def test_bad_output_is_repaired_with_the_reason(env):
    cards, client = run(env, reply("not json at all"), cards_reply(card()))
    assert cards and "Rejected" in client.calls[1]["messages"][-1]["content"]
    assert env.audit.kinds().count("cards_rejected") == 1


@pytest.mark.parametrize("bad,needle", [
    (card(endpoint=BYID), "not runnable"),
    (card(endpoint=ADMIN_LOGS), "not runnable"),
    (card(endpoint="GET /api/invented"), "not runnable"),
    (card(risk_id="made-up-risk"), "unknown risk_id"),
])
def test_cards_that_would_break_downstream_are_rejected(env, bad, needle):
    _, client = run(env, cards_reply(bad), cards_reply(card()))
    assert needle in client.calls[1]["messages"][-1]["content"]


def test_schema_violations_are_caught(env):
    _, client = run(env, cards_reply(card(load_multiple=0)), cards_reply(card()))
    assert "load_multiple" in client.calls[1]["messages"][-1]["content"]


def test_duplicate_ids_and_too_many_cards_are_rejected(env):
    dup = [card(), card()]
    many = [card(id=f"card-{i}") for i in range(6)]
    assert any("more than once" in p for p in check_cards([Card(**c) for c in dup], env.brief, 5))
    assert any("at most 5" in p for p in check_cards([Card(**c) for c in many], env.brief, 5))


def test_load_size_is_not_the_testers_problem(env):
    cards, _ = run(env, cards_reply(card(load_multiple=9.0)))       # above max_multiple: the Guardrail clamps it
    assert cards[0].load_multiple == 9.0


def test_gives_up_loudly_after_the_call_budget(env):
    with pytest.raises(DesignFailed, match="no valid cards after 2 model calls"):
        run(env, reply("nope"), reply("still nope"))


@pytest.mark.parametrize("stop,needle", [("refusal", "refused"), ("max_tokens", "max_tokens")])
def test_refusal_and_truncation_stop_the_run(env, stop, needle):
    with pytest.raises(DesignFailed, match=needle):
        run(env, reply("", stop=stop))


def test_a_brief_with_nothing_runnable_fails_before_calling_the_model(env):
    env.brief.output.endpoints[:] = [n for n in env.brief.output.endpoints if n.key == BYID]
    client = ScriptedClient()
    with pytest.raises(DesignFailed, match="no runnable endpoint"):
        design_cards(env.m, client, env.brief, CAP, env.audit)
    assert client.calls == []


# --- feedback: the Validator sent a card back as an unfair test ----------------

def flawed() -> Feedback:
    return Feedback(Card(**card()), "only 12% of the target load was delivered: load was too small to stress anything")


def test_feedback_is_shown_to_the_model_and_ids_must_be_kept(env):
    cards, client = run(env, cards_reply(card(load_multiple=2.5)), feedback=[flawed()])
    sent = client.calls[0]["messages"][0]["content"]
    assert "why_unfair" in sent and "12% of the target load" in sent and '"max_cards": 1' in sent
    assert [c.id for c in cards] == ["login-overload"]


def test_feedback_mode_rejects_renamed_or_extra_cards(env):
    _, client = run(env, cards_reply(card(id="brand-new-id")), cards_reply(card()), feedback=[flawed()])
    assert "must keep their original ids" in client.calls[1]["messages"][-1]["content"]
    with pytest.raises(DesignFailed):
        run(env, cards_reply(card(), card(id="second-card")), cards_reply(card(), card(id="second-card")), feedback=[flawed()])
