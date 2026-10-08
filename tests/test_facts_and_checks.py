# tests/test_facts_and_checks.py
import json
from pathlib import Path

import pytest

from swarm.audit import AuditLog
from swarm.brief import Evidence, EndpointNote, PlannerOutput, Recipe, Risk
from swarm.checks import check_output
from swarm.tools.dbschema import read_db_schema
from swarm.tools.explorer import CodeJail
from swarm.tools.openapi import index_by_key, summarize_openapi

SPEC = json.loads(Path("tests/data/openapi_sample.json").read_text(encoding="utf-8"))
SUMMARY = summarize_openapi(SPEC)
INDEX = index_by_key(SUMMARY)
DB = {"tables": {"users": {"columns": [], "indexes": [], "rows": 50000}}}

LOGIN = "POST /api/login"
ME = "GET /api/me"
USERS = "GET /api/users"
BYID = "GET /api/users/{user_id}"


# --- OpenAPI summary (deterministic facts) -----------------------------------

def test_summary_lists_every_endpoint_with_auth_and_params():
    assert {LOGIN, ME, USERS, BYID, "GET /health"} <= set(INDEX)
    assert INDEX[ME]["auth_required"] and not INDEX["GET /health"]["auth_required"]
    assert INDEX[BYID]["path_params"][0]["name"] == "user_id"
    assert {q["name"] for q in INDEX[USERS]["query_params"]} == {"skip", "limit"}


def test_summary_inlines_request_schemas():
    body = INDEX[LOGIN]["request_body"]
    assert "form-urlencoded" in body["content_type"] and "username" in body["schema"]["properties"]
    assert "$ref" not in json.dumps(INDEX[LOGIN])


# --- DB schema reader, against a scripted sandbox ----------------------------

class FakeDb:
    def psql(self, sql, **kw):
        if "information_schema.columns" in sql:
            return json.dumps([{"table_name": "users", "column_name": "email", "data_type": "character varying",
                                "udt_name": "varchar", "is_nullable": "NO", "column_default": None},
                               {"table_name": "users", "column_name": "role", "data_type": "USER-DEFINED",
                                "udt_name": "user_role_enum", "is_nullable": "NO", "column_default": "'USER'"}])
        if "pg_indexes" in sql:
            return json.dumps([{"table_name": "users", "indexname": "ix_users_email", "indexdef": "CREATE UNIQUE INDEX ..."}])
        return "50000"


def test_db_schema_shape():
    t = read_db_schema(FakeDb())["tables"]["users"]
    assert t["rows"] == 50000 and [c["name"] for c in t["columns"]] == ["email", "role"]
    assert t["columns"][1]["type"] == "user_role_enum" and t["indexes"] == ["CREATE UNIQUE INDEX ..."]


# --- checks -------------------------------------------------------------------

@pytest.fixture
def jail(tmp_path):
    root = tmp_path / "code"
    (root / "app").mkdir(parents=True)
    (root / "app" / "security.py").write_text(
        "pwd_context = CryptContext(schemes=[\"bcrypt\"])\n\ndef verify_password(plain, hashed):\n    return pwd_context.verify(plain, hashed)\n",
        encoding="utf-8")
    return CodeJail(root, AuditLog())


def code_ev(quote="pwd_context = CryptContext", ref="app/security.py", **kw):
    return Evidence(kind="code", ref=ref, quote=quote, **kw)


LOGIN_RECIPE = Recipe(auth_role="none", form_body={"username": "{{user.email}}", "password": "{{user.password}}"})
ME_RECIPE = Recipe(auth_role="user")


def output(notes, risks=None, baseline=(ME,)):
    return PlannerOutput(summary="a login service backed by Postgres", endpoints=notes, baseline_targets=list(baseline),
                         risks=risks or [], unknowns=[])


def note(key, suit="good", recipe=None, ev=None):
    return EndpointNote(key=key, load_suitability=suit, writes_data=False, cost_notes="x", recipe=recipe, evidence=ev or [])


def run(out, jail):
    return check_output(out, SUMMARY, DB, jail, jail.audit)


def test_clean_output_passes_untouched(jail):
    res = run(output([note(ME, recipe=ME_RECIPE, ev=[code_ev()]), note(LOGIN, recipe=LOGIN_RECIPE)]), jail)
    assert res.dropped == [] and res.blocking == [] and res.output.baseline_targets == [ME]


def test_unknown_and_duplicate_endpoints_are_dropped(jail):
    res = run(output([note(ME, recipe=ME_RECIPE), note("GET /api/invented"), note(ME, recipe=ME_RECIPE)]), jail)
    assert [n.key for n in res.output.endpoints] == [ME] and len(res.dropped) == 2


def test_fabricated_quote_is_dropped_real_quote_kept(jail):
    res = run(output([note(ME, recipe=ME_RECIPE, ev=[code_ev(), code_ev(quote="pool_size = 99")])]), jail)
    assert len(res.output.endpoints[0].evidence) == 1 and "quote not found" in res.dropped[0]


def test_quote_must_sit_inside_the_cited_lines(jail):
    ok = run(output([note(ME, recipe=ME_RECIPE, ev=[code_ev("return pwd_context.verify", line_start=4, line_end=4)])]), jail)
    bad = run(output([note(ME, recipe=ME_RECIPE, ev=[code_ev("return pwd_context.verify", line_start=1, line_end=1)])]), jail)
    assert ok.dropped == [] and bad.dropped


def test_evidence_in_files_outside_the_jail_is_dropped(jail):
    res = run(output([note(ME, recipe=ME_RECIPE, ev=[code_ev(ref="../secrets.py"), code_ev(ref=".env")])]), jail)
    assert res.output.endpoints[0].evidence == [] and len(res.dropped) == 2


def test_authed_endpoint_with_no_auth_recipe_is_not_runnable(jail):
    res = run(output([note(ME, recipe=Recipe(auth_role="none")), note(LOGIN, recipe=LOGIN_RECIPE)], baseline=(ME, LOGIN)), jail)
    me = next(n for n in res.output.endpoints if n.key == ME)
    assert me.recipe is None and me.load_suitability == "needs_data"
    assert res.output.baseline_targets == [LOGIN]


def test_recipe_body_is_checked_against_the_schema(jail):
    wrong = Recipe(auth_role="none", form_body={"username": "x"})              # password missing
    res = run(output([note(LOGIN, recipe=wrong), note(ME, recipe=ME_RECIPE)]), jail)
    assert next(n for n in res.output.endpoints if n.key == LOGIN).recipe is None
    assert any("schema" in d for d in res.dropped)


def test_unknown_query_params_rejected(jail):
    res = run(output([note(USERS, recipe=Recipe(auth_role="admin", query={"bogus": 1})), note(ME, recipe=ME_RECIPE)]), jail)
    assert next(n for n in res.output.endpoints if n.key == USERS).recipe is None


def test_path_param_endpoints_are_forced_to_needs_data(jail):
    res = run(output([note(BYID, recipe=Recipe(auth_role="admin")), note(ME, recipe=ME_RECIPE)]), jail)
    n = next(n for n in res.output.endpoints if n.key == BYID)
    assert (n.load_suitability, n.recipe) == ("needs_data", None)


def test_good_without_recipe_is_downgraded(jail):
    res = run(output([note(ME), note(LOGIN, recipe=LOGIN_RECIPE)], baseline=(LOGIN,)), jail)
    assert next(n for n in res.output.endpoints if n.key == ME).load_suitability == "needs_data"


def test_no_runnable_baseline_target_blocks(jail):
    res = run(output([note(ME, suit="avoid", recipe=ME_RECIPE)], baseline=(ME,)), jail)
    assert res.blocking and "baseline" in res.blocking[0]


def risk(endpoints=(ME,), ev=None, id="hash-cost"):
    return Risk(id=id, statement="password hashing is expensive per request", endpoints=list(endpoints),
                signals=["api_cpu"], confidence="medium", evidence=ev or [code_ev()])


def test_risks_need_valid_evidence_and_known_endpoints(jail):
    res = run(output([note(ME, recipe=ME_RECIPE)],
                     risks=[risk(), risk(id="no-proof", ev=[code_ev(quote="never written")]),
                            risk(id="ghost", endpoints=("GET /api/nope",))]), jail)
    assert [r.id for r in res.output.risks] == ["hash-cost"]
