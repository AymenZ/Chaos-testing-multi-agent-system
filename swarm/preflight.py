# swarm/preflight.py
import json
from dataclasses import dataclass, field

from adapters.sandbox import Sandbox, SandboxError
from adapters.seed import seed
from swarm.manifest import Manifest


class PreflightError(Exception):
    pass


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    failed_step: str | None = None
    reason: str | None = None
    details: dict = field(default_factory=dict)


def _url(base, path: str) -> str:
    return str(base).rstrip("/") + path


def make_sandbox(m: Manifest) -> Sandbox:
    return Sandbox(m.target.compose_file, m.target.api_service, m.target.db_service,
                   env=m.compose_env)


def check_isolation(sandbox: Sandbox) -> dict:
    """The safety claim, tested: nothing in the sandbox may reach the internet."""
    if sandbox.has_internet():
        raise PreflightError("sandbox can reach the internet; the compose network must be internal")
    return {"isolated": True}


def check_login(m: Manifest, sandbox: Sandbox) -> dict:
    user = next(u for u in m.auth.seed_users if u.role == "user")
    url = _url(m.target.base_url, m.auth.login_path)
    if m.auth.login_encoding == "form":
        status, body = sandbox.request("POST", url, form={"username": user.email, "password": user.password})
    else:
        status, body = sandbox.request("POST", url, json_body={"email": user.email, "password": user.password})
    if status != 200:
        raise PreflightError(f"login returned {status}: {body[:200]}")
    try:
        has_token = m.auth.token_field in json.loads(body)
    except ValueError:
        has_token = False
    if not has_token:
        raise PreflightError(f"login response has no '{m.auth.token_field}' field")
    return {"login": "ok"}


def run_preflight(m: Manifest, sandbox: Sandbox) -> PreflightResult:
    health_url = _url(m.target.base_url, m.target.health_path)
    steps = [
        ("start_stack", sandbox.up),
        ("isolation", lambda: check_isolation(sandbox)),
        ("health", lambda: sandbox.wait_healthy(health_url, 60)),
        ("migrate", lambda: sandbox.exec(m.target.api_service, "alembic", "upgrade", "head")),
        ("seed", lambda: seed(m, sandbox)),
        ("login", lambda: check_login(m, sandbox)),
        ("snapshot", lambda: sandbox.snapshot(health_url)),     # the clean copy every reset returns to
    ]
    details: dict = {}
    for name, fn in steps:
        try:
            out = fn()
            if isinstance(out, dict):
                details.update(out)
        except (PreflightError, SandboxError) as e:
            return PreflightResult(ok=False, failed_step=name, reason=str(e), details=details)
    return PreflightResult(ok=True, details=details)
