# tests/test_planner.py
"""The agent loop, driven by a scripted fake model: no network, no API key."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from swarm.audit import AuditLog
from swarm.manifest import load_manifest
from swarm.planner import Facts, PlannerError, plan
from swarm.tools.explorer import CodeJail

SPEC = json.loads(Path("tests/data/openapi_sample.json").read_text(encoding="utf-8"))
DB = {"tables": {"users": {"columns": [], "indexes": [], "rows": 50000}}}


def use(id, name, **args):
    return NS(type="tool_use", id=id, name=name, input=args)


def reply(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop,
              usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=0))


class ScriptedClient:
    """Plays back canned responses and records what the agent sent each turn."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        assert self.script, "the agent made more model calls than the script has"
        return self.script.pop(0)


def good_brief() -> dict:
    ev = [dict(kind="code", ref="app/security.py", quote="pwd_context = CryptContext")]
    return dict(
        summary="A small login service with JWT auth, backed by one Postgres table.",
        endpoints=[
            dict(key="GET /api/me", load_suitability="good", writes_data=False, cost_notes="one primary-key lookup",
                 recipe=dict(auth_role="user"), evidence=ev),
            dict(key="POST /api/login", load_suitability="caution", writes_data=False, cost_notes="password verification",
                 recipe=dict(auth_role="none", form_body={"username": "{{user.email}}", "password": "{{user.password}}"}),
                 evidence=ev)],
        baseline_targets=["GET /api/me"],
        risks=[dict(id="hash-cost", statement="password verification is CPU heavy on every login", endpoints=["POST /api/login"],
                    signals=["api_cpu", "request_latency_p95"], confidence="medium", evidence=ev)],
        unknowns=["no rate limiting information found"])


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "code"
    (root / "app").mkdir(parents=True)
    (root / "app" / "security.py").write_text("pwd_context = CryptContext(schemes=['bcrypt'])\n", encoding="utf-8")
    (root / ".env").write_text("SECRET_KEY=hunter2\n", encoding="utf-8")
    audit = AuditLog()
    m = load_manifest("manifests/deployment_audit.yaml")
    m.llm.planner_max_steps = 5
    return NS(m=m, audit=audit, jail=CodeJail(root, audit), facts=Facts(lambda: SPEC, lambda: DB))


def run(env, *script):
    client = ScriptedClient(*script)
    return plan(env.m, client, env.facts, env.jail, env.audit), client


def tool_results(client, call_index):
    return [b for b in client.calls[call_index]["messages"][-1]["content"] if isinstance(b, dict) and b["type"] == "tool_result"]


def test_explore_then_submit_returns_a_brief(env):
    brief, client = run(env,
        reply(use("a", "get_api_summary"), use("b", "get_db_schema"), use("c", "list_files")),
        reply(use("d", "read_file", path="app/security.py"), use("e", "search_code", pattern="bcrypt")),
        reply(use("f", "submit_brief", **good_brief())))
    assert brief.output.baseline_targets == ["GET /api/me"] and brief.dropped == []
    assert brief.context.roles == ["admin", "user"] and brief.context.limits.max_rps == env.m.limits.max_rps
    assert env.audit.kinds().count("model_call") == 3 and "brief_accepted" in env.audit.kinds()
    # parallel tool calls come back together in ONE user message, followed by a step counter
    assert len(tool_results(client, 1)) == 3
    assert "steps left" in client.calls[1]["messages"][-1]["content"][-1]["text"]


def test_the_model_never_sees_passwords_or_secrets(env):
    _, client = run(env,
        reply(use("a", "read_file", path=".env")),
        reply(use("b", "submit_brief", **good_brief())))
    everything = repr(client.calls)
    assert "hunter2" not in everything and "sandbox-pass" not in everything
    denied = tool_results(client, 1)[0]
    assert denied["is_error"] and "blocked" in denied["content"]
    assert "read_denied" in env.audit.kinds()


def test_schema_rejection_is_fed_back_and_the_agent_can_fix_it(env):
    bad = good_brief(); del bad["summary"]
    brief, client = run(env, reply(use("a", "submit_brief", **bad)), reply(use("b", "submit_brief", **good_brief())))
    rejection = tool_results(client, 1)[0]
    assert rejection["is_error"] and "summary" in rejection["content"]
    assert brief.output.summary and "submit_rejected" in env.audit.kinds()


def test_unusable_brief_is_rejected_with_the_reason(env):
    weak = good_brief(); weak["endpoints"][0]["recipe"] = dict(auth_role="none")     # authed endpoint, no auth
    brief, client = run(env, reply(use("a", "submit_brief", **weak)), reply(use("b", "submit_brief", **good_brief())))
    assert "baseline" in tool_results(client, 1)[0]["content"]
    assert brief.output.baseline_targets == ["GET /api/me"]


def test_fabricated_evidence_is_dropped_but_the_brief_survives(env):
    b = good_brief(); b["endpoints"][0]["evidence"].append(dict(kind="code", ref="app/security.py", quote="made up quote"))
    brief, _ = run(env, reply(use("a", "submit_brief", **b)))
    assert len(brief.output.endpoints[0].evidence) == 1 and any("quote not found" in d for d in brief.dropped)


def test_budget_exhaustion_fails_loudly_and_warns_on_the_last_step(env):
    env.m.llm.planner_max_steps = 3
    client = ScriptedClient(*[reply(use(str(i), "list_files")) for i in range(3)])
    with pytest.raises(PlannerError, match="no accepted brief within 3 steps"):
        plan(env.m, client, env.facts, env.jail, env.audit)
    assert "budget_exhausted" in env.audit.kinds() and len(client.calls) == 3
    assert "last step" in client.calls[2]["messages"][-1]["content"][-1]["text"]


@pytest.mark.parametrize("stop,needle", [("refusal", "refused"), ("max_tokens", "max_tokens")])
def test_refusal_and_truncation_stop_the_run(env, stop, needle):
    with pytest.raises(PlannerError, match=needle):
        run(env, reply(stop=stop))


def test_talking_without_acting_gets_a_nudge_not_a_crash(env):
    brief, client = run(env, reply(NS(type="text", text="Let me think."), stop="end_turn"),
                        reply(use("a", "submit_brief", **good_brief())))
    assert "submit_brief" in client.calls[1]["messages"][-1]["content"] and brief


def test_unknown_tool_is_an_error_result_not_a_crash(env):
    _, client = run(env, reply(use("a", "rm_rf")), reply(use("b", "submit_brief", **good_brief())))
    assert tool_results(client, 1)[0]["is_error"]


def test_fact_tool_failure_is_a_planner_error(env):
    def boom():
        raise RuntimeError("sandbox is down")
    env.facts = Facts(boom, lambda: DB)
    with pytest.raises(PlannerError, match="sandbox is down"):
        run(env, reply(use("a", "get_api_summary")))


def test_every_call_uses_manifest_settings_and_keeps_thinking_blocks(env):
    thinking = NS(type="thinking", thinking="", signature="sig")
    _, client = run(env, reply(thinking, use("a", "list_files")), reply(use("b", "submit_brief", **good_brief())))
    first = client.calls[0]
    assert first["model"] == env.m.llm.planner_model and first["output_config"] == {"effort": env.m.llm.effort}
    assert any(t["name"] == "submit_brief" for t in first["tools"])
    assert "tool_choice" not in first                       # forced tool use is rejected by current models
    assistant = client.calls[1]["messages"][1]
    assert thinking in assistant["content"]                 # echoed back unchanged
