# tests/test_brief.py
"""The planner's output contract tolerates the slips real models make, without weakening what it checks."""
from swarm.brief import MAX_QUOTE, Evidence, EndpointNote, PlannerOutput, Recipe


def output(**over):
    d = {"summary": "a summary that is long enough", "unknowns": [], "risks": [],
         "endpoints": [{"key": "GET /api/me", "load_suitability": "good", "cost_notes": "x"}],
         "baseline_targets": ["GET /api/me"]}
    d.update(over)
    return PlannerOutput.model_validate(d)


def test_null_query_means_no_query():
    assert Recipe.model_validate({"auth_role": "user", "query": None}).query == {}


def test_baseline_targets_accept_objects_and_strings():
    out = output(baseline_targets=[{"method": "GET", "path": "/api/me"}, {"key": "GET /api/users"}, "GET /api/items"])
    assert out.baseline_targets == ["GET /api/me", "GET /api/users", "GET /api/items"]


def test_a_long_quote_is_cut_to_its_start_not_rejected():
    long = "x" * 450
    e = Evidence.model_validate({"ref": "app/a.py", "quote": long})
    assert e.quote == long[:MAX_QUOTE] and long.startswith(e.quote)


def test_a_missing_writes_data_is_inferred_safely_from_the_method():
    def note(key):
        return EndpointNote.model_validate({"key": key, "load_suitability": "caution", "cost_notes": "x"})
    assert note("GET /a").writes_data is False
    assert note("POST /a").writes_data is True and note("DELETE /a").writes_data is True


def test_an_explicit_writes_data_is_never_overridden():
    n = EndpointNote.model_validate({"key": "POST /a", "load_suitability": "good", "writes_data": False, "cost_notes": "x"})
    assert n.writes_data is False


def test_the_step_12_failure_shape_now_validates():
    out = output(endpoints=[{"key": "GET /api/me", "load_suitability": "good", "cost_notes": "x",
                             "recipe": {"auth_role": "user", "query": None},
                             "evidence": [{"ref": "app/a.py", "quote": "y" * 400}]}],
                 baseline_targets=[{"auth_role": "user", "key": "GET /api/me"}])
    assert out.baseline_targets == ["GET /api/me"] and len(out.endpoints[0].evidence[0].quote) == MAX_QUOTE


def test_json_syntax_leaked_into_an_endpoint_key_is_repaired():
    n = EndpointNote.model_validate({"key": 'delete /api/users/{user_id}",load_suitability:', "load_suitability": "avoid",
                                     "writes_data": True, "cost_notes": "x"})
    assert n.key == "DELETE /api/users/{user_id}"
    out = output(baseline_targets=['GET /api/me",x'])
    assert out.baseline_targets == ["GET /api/me"]


def test_risk_endpoint_keys_are_repaired_too():
    from swarm.brief import Risk
    r = Risk.model_validate({"id": "some-risk", "statement": "a statement that is long enough", "endpoints": ['GET /a",'],
                             "signals": ["api_cpu"], "confidence": "low", "evidence": [{"ref": "a.py", "quote": "abc"}]})
    assert r.endpoints == ["GET /a"]
