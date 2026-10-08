# tests/test_manifest.py
import copy
import pytest
import yaml
from pydantic import ValidationError
from swarm.manifest import load_manifest

VALID = {
    "target": {"compose_file": "c.yml", "api_service": "api", "db_service": "db",
               "base_url": "http://api:8000"},
    "auth": {"login_path": "/api/login", "login_encoding": "form",
             "seed_users": [{"email": "u@sandbox.test", "password": "sandbox-pass-0", "role": "user"}]},
    "seed": {"method": "direct_insert", "rows": {"users": 1000}},
    "limits": {"max_rps": 300, "max_duration_s": 180, "max_vus": 500, "request_timeout_s": 5},
    "rules": {"default": {"p95_vs_baseline_max": 2.0, "error_rate_max": 0.01, "recovery_max_s": 30}},
    "baseline": {"repeats": 5, "warmup_s": 15, "measure_s": 60},
    "resources": {"api_cpus": 1, "db_cpus": 1, "loadgen_cpus": 1, "host_cpu_limit_pct": 85},
    "budgets": {"max_experiments": 10, "max_attempts": 3, "wall_clock_s": 5400, "max_llm_calls": 20},
}

def write(tmp_path, data):
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(data))
    return p

def test_valid_manifest_loads(tmp_path):
    m = load_manifest(write(tmp_path, VALID))
    assert m.allowed_hosts == {"api"}

def test_path_without_leading_slash_rejected(tmp_path):
    bad = copy.deepcopy(VALID); bad["target"]["health_path"] = "health"
    with pytest.raises(ValidationError):
        load_manifest(write(tmp_path, bad))

def test_real_manifest_points_at_an_existing_compose_file():
    m = load_manifest("manifests/deployment_audit.yaml")
    assert m.target.compose_file.exists()
    assert m.compose_env == {"API_CPUS": "1", "DB_CPUS": "1", "LOADGEN_CPUS": "1"}

def test_compose_path_is_relative_to_manifest(tmp_path):
    m = load_manifest(write(tmp_path, VALID))
    assert m.target.compose_file == (tmp_path / "c.yml").resolve()

def test_negative_limit_rejected(tmp_path):
    bad = copy.deepcopy(VALID); bad["limits"]["max_rps"] = -5
    with pytest.raises(ValidationError):
        load_manifest(write(tmp_path, bad))

def test_missing_section_rejected(tmp_path):
    bad = copy.deepcopy(VALID); del bad["auth"]
    with pytest.raises(ValidationError):
        load_manifest(write(tmp_path, bad))

def test_typo_key_rejected(tmp_path):
    bad = copy.deepcopy(VALID); bad["limits"]["max_rsp"] = 100
    with pytest.raises(ValidationError):
        load_manifest(write(tmp_path, bad))