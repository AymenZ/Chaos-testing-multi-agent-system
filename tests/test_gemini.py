# tests/test_gemini.py
"""The Gemini adapter against a scripted fake of Google's client. Real google.genai types are used for
everything the SDK would return, so the translation is checked against the real shapes."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from google.genai import errors, types

from adapters.gemini import BlockList, GeminiClient, GeminiError, RateLimiter, clean_schema
from swarm.audit import AuditLog
from swarm.baseline import CapacityContext
from swarm.card import CardSet
from swarm.llm import MissingKeyError, make_client
from swarm.manifest import load_manifest
from swarm.planner import Facts, _tools, plan
from swarm.tester import design_cards
from swarm.tools.explorer import CodeJail
from tests.test_planner import DB, SPEC, good_brief
from tests.test_tester import card, make_brief


# --- fakes -------------------------------------------------------------------------------------

def gresp(*parts, finish="STOP", blocked=None, prompt=10, out=5, thoughts=0, cached=0):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)),
                                    finish_reason=types.FinishReason(finish))],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=blocked) if blocked else None,
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=prompt, candidates_token_count=out,
                                           thoughts_token_count=thoughts, cached_content_token_count=cached))


def call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def text(t):
    return types.Part(text=t)


class FakeGenai:
    """Plays back scripted responses (or raises scripted errors) and records every request."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.requests.append(NS(model=model, contents=list(contents), config=config))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*script, **kw):
    sleeps = []
    c = GeminiClient(client=FakeGenai(*script), sleep=sleeps.append, backoff_s=10.0, **kw)
    c.sleeps = sleeps
    return c


def api_error(code, status="X", message="boom"):
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, {"error": {"code": code, "message": message, "status": status}})


TOOLS = [{"name": "list_files", "description": "list", "input_schema": {"type": "object", "properties": {}}},
         {"name": "read_file", "description": "read", "input_schema": {
             "type": "object", "title": "x", "additionalProperties": False, "required": ["path", "ghost"],
             "properties": {"path": {"type": "string", "title": "Path", "default": "a"}}}}]


def create(c, messages=None, **kw):
    return c.messages.create(model="gemini-test", max_tokens=1000, messages=messages or [{"role": "user", "content": "hi"}], **kw)


# --- schema sanitising -----------------------------------------------------------------------------

def test_clean_schema_drops_what_gemini_may_reject_and_keeps_the_rest():
    out = clean_schema({"type": "object", "title": "T", "additionalProperties": False, "required": ["a", "ghost"],
                        "properties": {"a": {"type": "string", "title": "A", "default": "x", "pattern": "^a", "minLength": 3,
                                             "description": "d", "enum": ["x", "y"]},
                                       "n": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "maybe"},
                                       "l": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1}}})
    assert out == {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "string", "description": "d", "enum": ["x", "y"]},
        "n": {"type": ["string", "null"], "description": "maybe"},
        "l": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1}}}


def _keys(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            if k != "properties":
                _keys(v, found)
            else:
                [_keys(sub, found) for sub in v.values()]
    elif isinstance(node, list):
        [_keys(v, found) for v in node]
    return found


def test_the_real_agent_schemas_come_out_clean():
    submit = next(t for t in _tools() if t["name"] == "submit_brief")["input_schema"]
    for schema in (submit, __import__("anthropic").transform_schema(CardSet)):
        cleaned = clean_schema(schema)
        assert cleaned["properties"] and _keys(cleaned) <= {"type", "properties", "required", "description", "enum",
                                                            "items", "anyOf", "minimum", "maximum", "minItems", "maxItems"}


# --- request translation ---------------------------------------------------------------------------

def test_config_carries_system_tokens_tools_and_disables_automatic_calling():
    c = client(gresp(text("ok")))
    create(c, system="be brief", tools=TOOLS, cache_control={"type": "ephemeral"}, output_config={"effort": "high"})
    cfg = c._client.requests[0].config
    assert cfg.system_instruction == "be brief" and cfg.max_output_tokens == 1000
    assert cfg.automatic_function_calling.disable is True
    declared = cfg.tools[0].function_declarations
    assert [d.name for d in declared] == ["list_files", "read_file"]
    assert declared[0].parameters_json_schema is None                       # no-argument tool
    assert declared[1].parameters_json_schema == {"type": "object", "required": ["path"],
                                                  "properties": {"path": {"type": "string"}}}


def test_structured_output_becomes_a_json_schema_request():
    c = client(gresp(text('{"cards": []}')))
    create(c, output_config={"effort": "medium", "format": {"type": "json_schema", "schema": {
        "type": "object", "title": "X", "additionalProperties": False, "properties": {"a": {"type": "string"}}}}})
    cfg = c._client.requests[0].config
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == {"type": "object", "properties": {"a": {"type": "string"}}}


# --- reply translation -------------------------------------------------------------------------------

def test_a_text_reply_and_its_usage():
    r = create(client(gresp(text("hel"), text("lo"), prompt=100, out=20, thoughts=7, cached=40)))
    assert [b.type for b in r.content] == ["text"] and r.content[0].text == "hello"
    assert r.stop_reason == "end_turn"
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_read_input_tokens) == (100, 27, 40)


def test_function_calls_become_tool_use_blocks_with_ids_and_reasoning_is_hidden():
    thought = types.Part(text="thinking...", thought=True)
    r = create(client(gresp(thought, call("read_file", path="a.py"), call("list_files"))))
    uses = [b for b in r.content if b.type == "tool_use"]
    assert r.stop_reason == "tool_use" and [(u.name, u.input) for u in uses] == [("read_file", {"path": "a.py"}), ("list_files", {})]
    assert len({u.id for u in uses}) == 2 and not any(b.type == "text" for b in r.content)


def test_tool_results_go_back_as_function_responses_and_the_original_turn_is_kept_for_its_signatures():
    signed = types.Part(function_call=types.FunctionCall(name="read_file", args={"path": "a.py"}), thought_signature=b"sig")
    c = client(gresp(signed), gresp(text("done")))
    first = create(c)
    use = first.content[0]
    history = [{"role": "user", "content": "go"}, {"role": "assistant", "content": first.content},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id, "content": "file text"},
                                            {"type": "text", "text": "9 steps left."}]}]
    create(c, messages=history)
    contents = c._client.requests[1].contents
    assert contents[1] is first.content.raw and contents[1].parts[0].thought_signature == b"sig"
    answer = contents[2].parts
    assert contents[2].role == "user" and answer[0].function_response.name == "read_file"
    assert answer[0].function_response.response == {"result": "file text"} and answer[1].text == "9 steps left."


def test_error_results_use_the_error_key_and_unknown_ids_fail_loudly():
    c = client(gresp(call("read_file", path="x")), gresp(text("ok")))
    use = create(c).content[0]
    create(c, messages=[{"role": "assistant", "content": BlockList([use])},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id, "content": "denied", "is_error": True}]}])
    assert c._client.requests[1].contents[1].parts[0].function_response.response == {"error": "denied"}
    with pytest.raises(GeminiError, match="unknown call id"):
        create(client(gresp(text("x"))), messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "nope", "content": "x"}]}])


@pytest.mark.parametrize("response,stop", [
    (gresp(text("x"), finish="SAFETY"), "refusal"),
    (gresp(text("x"), finish="PROHIBITED_CONTENT"), "refusal"),
    (gresp(text("x"), finish="MAX_TOKENS"), "max_tokens"),
    (gresp(finish="MALFORMED_FUNCTION_CALL"), "end_turn"),
    (types.GenerateContentResponse(candidates=[], prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY")), "refusal"),
])
def test_finish_reasons_map_to_the_stop_reasons_the_agents_understand(response, stop):
    assert create(client(response)).stop_reason == stop


# --- the free tier: pacing and retries --------------------------------------------------------------------

def test_rate_limiter_keeps_calls_apart():
    now, sleeps = [100.0], []
    limiter = RateLimiter(10, clock=lambda: now[0], sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)))
    limiter.wait(); now[0] += 2.0; limiter.wait(); limiter.wait()
    assert sleeps == [pytest.approx(4.0), pytest.approx(6.0)]                 # 10 per minute = one every 6 s


def test_429_is_retried_with_the_delay_google_asks_for_then_backs_off():
    c = client(api_error(429, message="retryDelay: 12s"), api_error(429), gresp(text("ok")))
    assert create(c).content[0].text == "ok"
    assert c.sleeps == [12.0, 20.0]                                           # asked 12 s; then own backoff doubled to 20


def test_server_errors_are_retried_too():
    assert create(client(api_error(503, "UNAVAILABLE"), gresp(text("ok")))).stop_reason == "end_turn"


def test_a_daily_quota_error_stops_immediately():
    c = client(api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded: requests per day"))
    with pytest.raises(GeminiError, match="daily quota"):
        create(c)
    assert c.sleeps == []


def test_bad_requests_and_exhausted_retries_raise_a_clear_error():
    with pytest.raises(GeminiError, match="400"):
        create(client(api_error(400, "INVALID_ARGUMENT", "bad schema")))
    with pytest.raises(GeminiError, match="429"):
        create(client(*[api_error(429)] * 3, max_retries=2))


# --- the real agents on top of the adapter ----------------------------------------------------------------

@pytest.fixture
def m():
    return load_manifest("manifests/deployment_audit.yaml")


def test_the_real_planner_runs_unchanged_on_gemini(m, tmp_path):
    root = tmp_path / "code"
    (root / "app").mkdir(parents=True)
    (root / "app" / "security.py").write_text("pwd_context = CryptContext(schemes=['bcrypt'])\n", encoding="utf-8")
    c = client(gresp(call("get_api_summary"), call("list_files")),
               gresp(call("read_file", path="app/security.py")),
               gresp(call("submit_brief", **good_brief())))
    audit = AuditLog()
    brief = plan(m, c, Facts(lambda: SPEC, lambda: DB), CodeJail(root, audit), audit)
    assert brief.output.baseline_targets == ["GET /api/me"]
    second = c._client.requests[1].contents                                    # the tool results travelled back correctly
    assert [p.function_response.name for p in second[-1].parts if p.function_response] == ["get_api_summary", "list_files"]
    assert audit.kinds().count("model_call") == 3


def test_the_real_tester_runs_unchanged_on_gemini(m):
    c = client(gresp(text(json.dumps({"cards": [card()]}))))
    cards = design_cards(m, c, make_brief(), CapacityContext(250, "exact", 4.0), AuditLog())
    assert cards[0].id == "login-overload"
    assert c._client.requests[0].config.response_mime_type == "application/json"


# --- choosing the client from the manifest ------------------------------------------------------------------

def test_make_client_needs_a_key_and_picks_the_provider(m, monkeypatch):
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingKeyError, match="GEMINI_API_KEY"):
        make_client(m)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert isinstance(make_client(m), GeminiClient)
    m.llm.provider = "anthropic"
    with pytest.raises(MissingKeyError, match="ANTHROPIC_API_KEY"):
        make_client(m)


# --- visibility: a slow or stuck call must never look like a silent hang -----------------------------------

def test_every_request_carries_a_timeout():
    c = client(gresp(text("ok")))
    create(c)
    assert c._client.requests[0].config.http_options.timeout == 90_000


def test_retries_announce_themselves():
    logs = []
    c = GeminiClient(client=FakeGenai(api_error(429, "RESOURCE_EXHAUSTED", "retryDelay: 12s"), gresp(text("ok"))),
                     sleep=lambda s: None, log=logs.append)
    create(c)
    assert len(logs) == 1 and "429" in logs[0] and "waiting 12s" in logs[0] and "retry 1/2" in logs[0]


def test_a_timeout_is_retried_and_announced_while_other_exceptions_are_not():
    logs = []
    c = GeminiClient(client=FakeGenai(TimeoutError("read timed out"), gresp(text("ok"))), sleep=lambda s: None, log=logs.append)
    assert create(c).content[0].text == "ok" and "timed out after 90s" in logs[0]
    with pytest.raises(GeminiError, match="ValueError"):
        create(GeminiClient(client=FakeGenai(ValueError("bad")), sleep=lambda s: None, log=logs.append))


def test_the_planner_announces_each_request_before_waiting_on_it(m, tmp_path):
    root = tmp_path / "code"
    (root / "app").mkdir(parents=True)
    (root / "app" / "security.py").write_text("pwd_context = CryptContext(schemes=['bcrypt'])\n", encoding="utf-8")
    seen = []
    audit = AuditLog(echo=lambda r: seen.append(r["kind"]))
    c = client(gresp(call("list_files")), gresp(call("submit_brief", **good_brief())))
    plan(m, c, Facts(lambda: SPEC, lambda: DB), CodeJail(root, audit), audit)
    assert seen[:3] == ["model_request", "model_call", "tool_call"]


# --- the free-tier budget: per-model pacing and a daily counter ---------------------------------------------

def budgeted(tmp_path, *script, limits=None, today=lambda: "2026-10-28", **kw):
    sleeps = []
    c = GeminiClient(client=FakeGenai(*script), limits={"gemini-test": (15, 3)} if limits is None else limits, usage_path=tmp_path / "usage.json",
                     sleep=sleeps.append, today=today, log=lambda m: None, **kw)
    c.sleeps = sleeps
    return c


def test_the_daily_budget_stops_calls_before_they_waste_quota(tmp_path):
    c = budgeted(tmp_path, gresp(text("1")), gresp(text("2")), gresp(text("3")))
    for _ in range(3):
        create(c)
    assert c.usage_today("gemini-test") == (3, 3)
    with pytest.raises(GeminiError, match=r"daily budget for gemini-test is used up \(3 of 3"):
        create(c)
    assert len(c._client.requests) == 3                                     # the fourth never reached Google


def test_every_attempt_counts_including_retries_because_google_counts_them(tmp_path):
    c = budgeted(tmp_path, api_error(503, "UNAVAILABLE"), api_error(503, "UNAVAILABLE"), gresp(text("ok")))
    create(c)
    assert c.usage_today("gemini-test")[0] == 3


def test_the_count_survives_between_runs_and_resets_the_next_day(tmp_path):
    day = ["2026-10-28"]
    first_run = budgeted(tmp_path, gresp(text("a")), gresp(text("b")), today=lambda: day[0])
    create(first_run)
    create(first_run)
    second_run = budgeted(tmp_path, gresp(text("c")), today=lambda: day[0])      # a new process reads the saved file
    assert second_run.usage_today("gemini-test")[0] == 2
    create(second_run)
    assert second_run.usage_today("gemini-test")[0] == 3
    day[0] = "2026-10-29"
    assert second_run.usage_today("gemini-test")[0] == 0                         # a new day starts fresh


def test_each_model_is_paced_to_its_own_rate_and_has_its_own_budget(tmp_path):
    c = budgeted(tmp_path, gresp(text("a")), gresp(text("b")), gresp(text("c")),
                 limits={"fast": (60, 500), "slow": (5, 20)})
    now = [0.0]
    c._clock = lambda: now[0]
    c._sleep_fn = lambda s: now.__setitem__(0, now[0] + s)
    c._limiters.clear()
    for model in ("slow", "slow", "fast"):
        c.messages.create(model=model, max_tokens=10, messages=[{"role": "user", "content": "hi"}])
    assert now[0] == pytest.approx(12.0)                                    # 5 per minute = one every 12 s; "fast" waited 0
    assert c.usage_today("slow") == (2, 20) and c.usage_today("fast") == (1, 500)


def test_a_model_with_no_limits_is_unpaced_and_unbudgeted_but_still_counted(tmp_path):
    c = budgeted(tmp_path, gresp(text("a")), limits={})
    create(c)
    assert c.usage_today("gemini-test") == (1, None)


def test_a_corrupt_usage_file_just_starts_the_day_fresh(tmp_path):
    (tmp_path / "usage.json").write_text("{not json", encoding="utf-8")
    assert budgeted(tmp_path, gresp(text("a"))).usage_today("gemini-test")[0] == 0


# --- the manifest refuses models it has no limits for ----------------------------------------------------------

def test_the_manifest_requires_limits_for_the_gemini_models_it_uses():
    from pydantic import ValidationError
    from swarm.manifest import Manifest
    real = load_manifest("manifests/deployment_audit.yaml")
    data = real.model_dump(mode="json")
    data["llm"]["planner_model"] = "gemini-unlisted"
    with pytest.raises(ValidationError, match="no entry for 'gemini-unlisted'"):
        Manifest.model_validate(data)
    data["llm"]["planner_model"] = "gemini-3.8-flash"
    data["llm"]["limits"]["gemini-3.8-flash"] = {"rpm": 5, "rpd": 10}      # fewer a day than one run may use (12)
    with pytest.raises(ValidationError, match="allows only 10 calls a day"):
        Manifest.model_validate(data)
