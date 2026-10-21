# tests/test_guardrail.py
from swarm.baseline import CapacityContext
from swarm.brief import EndpointNote, Recipe
from swarm.card import Card
from swarm.guardrail import apply_guardrail
from swarm.manifest import load_manifest
from tests.test_tester import ADMIN_LOGS, BYID, LOGIN, ME, card, make_brief

import pytest

CAP = CapacityContext(rps=250, kind="exact", max_multiple=4.0)


@pytest.fixture
def env():
    m = load_manifest("manifests/deployment_audit.yaml")      # max_rps 1000, 180 s max, 5 s timeout, 15 s warmup
    return m, make_brief()


def run(env, *cards, cap=CAP, already=0):
    m, brief = env
    return apply_guardrail([Card(**c) for c in cards], brief, cap, m, already_planned=already)


def test_cards_become_exact_numbers(env):
    r = run(env, card(load_multiple=2.0, hold_s=60, ramp_s=10))
    e = r.experiments[0]
    assert r.rejected == [] and e.adjustments == []
    assert (e.target_rps, e.warmup_rps, e.warmup_s, e.ramp_s, e.hold_s) == (500, 125, 15, 10, 60)
    assert (e.method, e.path, e.auth_role) == ("POST", "/api/login", "none")
    assert e.total_s == 85


def test_the_recipe_is_attached_with_placeholders_intact(env):
    e = run(env, card()).experiments[0]
    assert e.form_body == {"username": "{{user.email}}", "password": "{{user.password}}"}
    assert "sandbox-pass" not in e.model_dump_json()


def test_too_much_load_is_clamped_and_noted(env):
    e = run(env, card(load_multiple=9.0)).experiments[0]
    assert e.target_rps == 1000 and any("9.0x -> 4.0x" in a for a in e.adjustments)


def test_the_rate_never_exceeds_the_manifest_even_if_capacity_data_is_off(env):
    odd = CapacityContext(rps=400, kind="exact", max_multiple=4.0)       # 4 x 400 = 1600 > max_rps
    assert run(env, card(load_multiple=4.0), cap=odd).experiments[0].target_rps == 1000


def test_duration_leaves_time_for_warmup_and_the_tail(env):
    e = run(env, card(ramp_s=30, hold_s=300)).experiments[0]
    assert e.ramp_s == 30 and e.hold_s == 130                 # 180 - 15 warmup - 5 tail = 160 for ramp + hold
    assert e.total_s + 5 <= 180 and any("only 160s fit" in a for a in e.adjustments)


def test_a_huge_ramp_gives_way_before_the_measured_hold_does(env):
    e = run(env, card(ramp_s=200, hold_s=60)).experiments[0]
    assert (e.ramp_s, e.hold_s) == (80, 60)                  # ramp capped at half of 160 s; hold untouched


def test_too_short_a_hold_is_raised(env):
    e = run(env, card(hold_s=3)).experiments[0]
    assert e.hold_s == 10 and any("hold 3s -> 10s" in a for a in e.adjustments)


@pytest.mark.parametrize("endpoint", [BYID, ADMIN_LOGS, "GET /api/invented"])
def test_unrunnable_endpoints_are_rejected(env, endpoint):
    r = run(env, card(endpoint=endpoint))
    assert r.experiments == [] and "not runnable" in r.rejected[0].reason


def test_experiment_budget_holds_across_rounds(env):
    cards = [card(id=f"card-{i}") for i in range(4)]
    r = run(env, *cards, already=8)                            # max_experiments is 10: two left
    assert [e.id for e in r.experiments] == ["card-0", "card-1"]
    assert [x.card_id for x in r.rejected] == ["card-2", "card-3"] and "budget" in r.rejected[0].reason


def test_a_path_that_would_change_the_host_is_refused(env):
    m, brief = env
    evil = EndpointNote(key="GET @evil.example/x", load_suitability="good", writes_data=False, cost_notes="x",
                        recipe=Recipe(auth_role="none"), evidence=[])
    brief.output.endpoints.append(evil)                      # "http://api:8000" + "@evil.example/x" -> host evil.example
    r = apply_guardrail([Card(**card(endpoint=evil.key))], brief, CAP, m)
    assert r.experiments == [] and "allowlist" in r.rejected[0].reason


def test_a_manifest_that_leaves_no_time_is_a_loud_error(env):
    m, brief = env
    m.limits.max_duration_s = 20                               # 20 - 15 warmup - 5 tail = 0
    with pytest.raises(ValueError, match="leaves only"):
        apply_guardrail([Card(**card())], brief, CAP, m)


def test_a_card_with_no_ramp_still_gets_the_one_second_minimum(env):
    e = run(env, card(ramp_s=0, hold_s=60)).experiments[0]
    assert e.ramp_s == 1 and e.total_s == 15 + 1 + 60
