import pytest
from pydantic import ValidationError

from swarm.guardrail import check_envelope, lint_against_openapi
from swarm.manifest import SLO, Manifest
from swarm.results import Baseline, HostStats
from swarm.spec import Step
from swarm.validator import Flaw, validate_run
from swarm.verdict import Confirm, Outcome, judge, median_run, resolve_repeat
from tests.conftest import make_baseline, make_metrics, make_spec

SLO_DEFAULT = SLO()  # 2x baseline, 1% errors

OPENAPI = {"paths": {
    "/api/login": {"post": {}},
    "/api/profile": {"get": {"security": [{"OAuth2PasswordBearer": []}]}},
    "/api/users/{user_id}": {"get": {"security": [{"OAuth2PasswordBearer": []}]}},
}}


# --- spec / manifest -------------------------------------------------------

def test_spec_rejects_full_url_and_unknown_fields():
    with pytest.raises(ValidationError):
        make_spec(steps=[Step(endpoint="http://evil.example/x", vus=1, duration_s=1)])
    with pytest.raises(ValidationError):
        make_spec(surprise="field")


def test_param_hash_ignores_id_and_prose_but_not_params():
    a = make_spec()
    b = make_spec(id="other-id-02", hypothesis="a completely different sentence")
    c = make_spec(steps=[Step(endpoint="/api/profile", auth="user", vus=31, duration_s=60)])
    assert a.param_hash() == b.param_hash()
    assert a.param_hash() != c.param_hash()


def test_manifest_loads_and_derives_allowlist(manifest: Manifest):
    assert manifest.envelope.allowed_hosts == ["api"]
    assert "default" in manifest.slos


# --- guardrail -------------------------------------------------------------

def test_guardrail_clamps_and_keeps_stimulus_reachable(manifest):
    spec = make_spec(steps=[Step(endpoint="/api/profile", auth="user", vus=500, duration_s=900)],
                     expected_stimulus={"min_achieved_vus": 450, "min_duration_s": 800})
    r = check_envelope(spec, manifest)
    assert r.accepted and len(r.clamped) == 2
    assert r.spec.steps[0].vus == manifest.envelope.max_vus
    assert r.spec.expected_stimulus.min_achieved_vus == manifest.envelope.max_vus
    assert spec.steps[0].vus == 500  # original untouched


def test_guardrail_rejects_destructive_and_unknown_slo(manifest):
    r = check_envelope(make_spec(destructive=True, slo_class="nope"), manifest)
    assert not r.accepted and len(r.violations) == 2 and r.spec is None


def test_openapi_lint_catches_missing_endpoint_and_auth_trap():
    assert lint_against_openapi(make_spec(), OPENAPI) == []
    missing = make_spec(steps=[Step(endpoint="/api/nope", vus=1, duration_s=1)])
    assert "not in the OpenAPI spec" in lint_against_openapi(missing, OPENAPI)[0]
    no_auth = make_spec(steps=[Step(endpoint="/api/profile", auth="none", vus=1, duration_s=1)])
    assert "401" in lint_against_openapi(no_auth, OPENAPI)[0]
    templated = make_spec(steps=[Step(endpoint="/api/users/123", auth="admin", vus=1, duration_s=1)])
    assert lint_against_openapi(templated, OPENAPI) == []


# --- baseline --------------------------------------------------------------

def test_baseline_band_and_noisy_widening():
    quiet = make_baseline((98, 100, 102))
    assert not quiet.noisy and quiet.p95_band_high == 102
    noisy = make_baseline((60, 100, 140))
    assert noisy.noisy and noisy.p95_band_high == 140 + 80  # widened, never tightened
    with pytest.raises(ValueError):
        Baseline.from_runs([make_metrics(), make_metrics()])


# --- validator -------------------------------------------------------------

def test_valid_run_has_no_flags():
    v = validate_run(make_spec(), make_metrics())
    assert v.valid and v.flags == []


@pytest.mark.parametrize("over,needle", [
    (dict(tool_crashed=True), "crashed"),
    (dict(metrics_missing_frac=0.5), "missing"),
    (dict(fault_confirmed=False), "fault"),
    (dict(host=HostStats(cpu_peak_pct=95, loadgen_cpu_peak_pct=30, mem_free_mb_min=2000)), "host CPU"),
    (dict(host=HostStats(cpu_peak_pct=50, loadgen_cpu_peak_pct=92, mem_free_mb_min=2000)), "load generator"),
    (dict(host=HostStats(cpu_peak_pct=50, loadgen_cpu_peak_pct=30, mem_free_mb_min=10, swapping=True)), "swapping"),
    (dict(duration_s=10), "ended early"),
])
def test_technical_flaws(over, needle):
    v = validate_run(make_spec(), make_metrics(**over))
    assert not v.valid and v.flaw is Flaw.TECHNICAL and needle in " ".join(v.reasons)


def test_401_burst_is_a_design_flaw_not_a_fast_app():
    m = make_metrics(status_counts={401: 9_000, 200: 1_000}, p95_ms=3)
    v = validate_run(make_spec(), m)
    assert not v.valid and v.flaw is Flaw.DESIGN


def test_5xx_and_low_vus_are_valid_target_behaviour():
    m = make_metrics(status_counts={200: 5_000, 500: 3_000, 0: 2_000}, achieved_vus=12, error_rate=0.5)
    v = validate_run(make_spec(), m)
    assert v.valid and any(f.startswith("low_achieved_vus") for f in v.flags)


def test_missing_signal_is_flagged_but_valid():
    v = validate_run(make_spec(), make_metrics(signals={"db_active_connections": 3}))
    assert v.valid and v.flags == ["mechanism_signal_missing:db_active_connections"]


# --- verdict ---------------------------------------------------------------

def test_clean_pass():
    v = judge(make_spec(), make_baseline(), make_metrics(p95_ms=110), SLO_DEFAULT)
    assert v.outcome is Outcome.PASS and v.confirm is Confirm.NONE


def test_pass_without_signals_is_weak_inconclusive_never_pass():
    v = judge(make_spec(), make_baseline(), make_metrics(signals={}), SLO_DEFAULT)
    assert v.outcome is Outcome.INCONCLUSIVE and v.weak_pass


def test_clear_fail_asks_for_repeat():
    v = judge(make_spec(), make_baseline(), make_metrics(p95_ms=400), SLO_DEFAULT)
    assert v.outcome is Outcome.FAIL and v.confirm is Confirm.REPEAT


def test_error_rate_fail():
    v = judge(make_spec(), make_baseline(), make_metrics(error_rate=0.2), SLO_DEFAULT)
    assert v.outcome is Outcome.FAIL


def test_over_ratio_but_inside_noise_band_is_inconclusive_not_fail():
    base = make_baseline((60, 100, 140))  # noisy: band high = 220, ratio limit = 200
    v = judge(make_spec(), base, make_metrics(p95_ms=210), SLO_DEFAULT)
    assert v.outcome is Outcome.INCONCLUSIVE and not v.weak_pass


def test_borderline_fail_asks_for_three_runs():
    v = judge(make_spec(), make_baseline(), make_metrics(p95_ms=215), SLO_DEFAULT)  # limit 200
    assert v.outcome is Outcome.FAIL and v.confirm is Confirm.MEDIAN3


def test_absolute_ceiling_overrides_baseline_slack():
    v = judge(make_spec(), make_baseline(), make_metrics(p95_ms=150), SLO(p95_ms_ceiling=120))
    assert v.outcome is Outcome.FAIL


def test_repeat_resolution():
    fail = judge(make_spec(), make_baseline(), make_metrics(p95_ms=400), SLO_DEFAULT)
    ok = judge(make_spec(), make_baseline(), make_metrics(), SLO_DEFAULT)
    assert resolve_repeat(fail, fail).outcome is Outcome.FAIL
    assert resolve_repeat(fail, ok).outcome is Outcome.INCONCLUSIVE


def test_median_run_picks_middle_p95():
    runs = [make_metrics(p95_ms=p) for p in (300, 100, 200)]
    assert median_run(runs).p95_ms == 200
