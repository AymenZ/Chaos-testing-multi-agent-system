# tests/test_preflight.py
"""Preflight, seeding and the sandbox's reset order, all against a fake: no Docker needed."""
import json

import pytest

from adapters.sandbox import Sandbox, SandboxError
from adapters.seed import seed
from swarm.manifest import load_manifest
from swarm.preflight import run_preflight

HASH = "$2b$12$" + "a" * 53


class FakeSandbox:
    """Records calls and answers like a healthy sandbox unless told otherwise."""

    def __init__(self, internet=False, login_status=200, user_count=None):
        self.calls, self.sql = [], []
        self.internet, self.login_status, self.user_count = internet, login_status, user_count

    def up(self): self.calls.append("up")
    def has_internet(self): self.calls.append("isolation"); return self.internet
    def wait_healthy(self, url, timeout_s=60): self.calls.append("health")
    def snapshot(self, url): self.calls.append("snapshot")

    def exec(self, service, *cmd, **kw):
        self.calls.append("migrate"); return ""

    def python(self, script, *args, **kw):
        return HASH

    def psql(self, sql, **kw):
        self.sql.append(sql)
        return str(self.user_count) if "count(*)" in sql else ""

    def request(self, method, url, **kw):
        self.calls.append("login")
        return self.login_status, json.dumps({"access_token": "t"} if self.login_status == 200 else {"detail": "no"})


@pytest.fixture
def m():
    return load_manifest("manifests/deployment_audit.yaml")


def test_happy_path_runs_every_step_in_order(m):
    fake = FakeSandbox(user_count=m.seed.rows["users"])
    result = run_preflight(m, fake)
    assert result.ok and result.details["users"] == m.seed.rows["users"]
    assert fake.calls == ["up", "isolation", "health", "migrate", "login", "snapshot"]


def test_internet_access_fails_before_anything_else_runs(m):
    fake = FakeSandbox(internet=True)
    result = run_preflight(m, fake)
    assert (result.ok, result.failed_step) == (False, "isolation")
    assert fake.calls == ["up", "isolation"]


def test_bad_login_fails_at_login_and_skips_snapshot(m):
    fake = FakeSandbox(login_status=401, user_count=m.seed.rows["users"])
    result = run_preflight(m, fake)
    assert (result.failed_step, "401" in result.reason) == ("login", True)
    assert "snapshot" not in fake.calls


def test_seed_count_mismatch_fails_at_seed(m):
    result = run_preflight(m, FakeSandbox(user_count=7))
    assert result.failed_step == "seed" and "expected" in result.reason


def test_seed_sql_uses_enum_labels_and_supplies_defaults(m):
    fake = FakeSandbox(user_count=m.seed.rows["users"])
    seed(m, fake)
    named, filler = fake.sql[0], fake.sql[1]
    assert "'ADMIN'::user_role_enum" in named and "gen_random_uuid()" in named and HASH in named
    assert f"generate_series(1, {m.seed.rows['users'] - len(m.auth.seed_users)})" in filler


def test_seed_rejects_unknown_tables(m):
    m.seed.rows["orders"] = 10
    with pytest.raises(SandboxError):
        seed(m, FakeSandbox())


def test_reset_order_is_stop_drop_clone_start(monkeypatch, tmp_path):
    sb = Sandbox(tmp_path / "c.yml")
    log = []
    monkeypatch.setattr(sb, "_run", lambda *a, **k: log.append(a[0]) or "")
    monkeypatch.setattr(sb, "psql", lambda sql, **k: log.append(sql.split()[0]))
    monkeypatch.setattr(sb, "wait_healthy", lambda url, timeout_s=60: log.append("health"))
    sb.reset("http://api:8000/health")
    assert log == ["stop", "DROP", "CREATE", "start", "health"]
