from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field , HttpUrl, field_validator, model_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
def _starts_with_slash(v: str) -> str:
    if not v.startswith("/"):
        raise ValueError("must start with '/' ")
    return v

class Target(Strict):
    compose_file: Path
    code_path: Path            # the target's source on this machine: built into the sandbox, read by the planner
    api_service: str
    db_service: str
    base_url: HttpUrl          # as seen from inside the sandbox network; the only host we talk to
    health_path: str = "/health"
    openapi_path: str = "/openapi.json"

    _check_paths = field_validator("health_path", "openapi_path")(_starts_with_slash)
    
class SeedUser(Strict):
    email: str
    password: str = Field(min_length=8,max_length=72)
    role: Literal["user","admin"]
    
class Auth(Strict):
    login_path: str
    login_encoding: Literal["form","json"]
    token_field: str = "access_token"
    seed_users: list[SeedUser] = Field(min_length=1)
    
    _check_login = field_validator("login_path")(_starts_with_slash)
    
    @model_validator(mode="after")
    def _users_ok(self):
        emails = [u.email for u in self.seed_users]
        if len(set(emails)) != len(emails):
            raise ValueError("seed_users emails must be unique")
        if not any(u.role == "user" for u in self.seed_users):
            raise ValueError("need at least one seed user with role 'user'")
        return self

class Seed(Strict):
    method: Literal["direct_insert"]
    rows: dict[str, int]

    @field_validator("rows")
    @classmethod
    def _rows_positive(cls, v):
        if not v or any(n <= 0 for n in v.values()):
            raise ValueError("rows must be non-empty with counts above zero")
        return v


class Limits(Strict):
    max_rps: int = Field(gt=0)
    max_duration_s: int = Field(gt=0)
    max_vus: int = Field(gt=0)
    request_timeout_s: float = Field(gt=0)


class RuleSet(Strict):
    p95_vs_baseline_max: float = Field(gt=1)
    error_rate_max: float = Field(ge=0, le=1)
    recovery_max_s: float = Field(gt=0)


class Rules(Strict):
    default: RuleSet


class Baseline(Strict):
    repeats: int = Field(ge=3, le=10)
    warmup_s: int = Field(ge=0)
    measure_s: int = Field(gt=0)


class Resources(Strict):
    api_cpus: int = Field(gt=0)
    db_cpus: int = Field(gt=0)
    loadgen_cpus: int = Field(gt=0)
    host_cpu_limit_pct: float = Field(gt=0, le=100)


class Budgets(Strict):
    max_experiments: int = Field(gt=0)
    max_attempts: int = Field(ge=1)
    wall_clock_s: int = Field(gt=0)
    max_llm_calls: int = Field(ge=0)    # 0 is fine for the no-LLM slice


class Llm(Strict):
    planner_model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    max_tokens: int = Field(gt=0)               # per model call
    planner_max_steps: int = Field(ge=2)        # model calls the planner may spend exploring + submitting


class Manifest(Strict):
    target: Target
    auth: Auth
    seed: Seed
    limits: Limits
    rules: Rules
    baseline: Baseline
    resources: Resources
    budgets: Budgets
    llm: Llm

    @model_validator(mode="after")
    def _cross_checks(self):
        if self.limits.request_timeout_s >= self.limits.max_duration_s:
            raise ValueError("request_timeout_s must be shorter than max_duration_s")
        if self.llm.planner_max_steps > self.budgets.max_llm_calls:
            raise ValueError("llm.planner_max_steps cannot exceed budgets.max_llm_calls")
        return self
    
    @property
    def allowed_hosts(self) -> set[str]:
        """The only hosts the runner may contact, derived from the URLs, never typed twice."""
        return {self.target.base_url.host}

    @property
    def compose_env(self) -> dict[str, str]:
        """Values handed to docker compose, so each is typed once, here. The planner reads the same
        code_path the sandbox builds, so its brief always describes the code under test."""
        r = self.resources
        return {
            "API_CPUS": str(r.api_cpus), "DB_CPUS": str(r.db_cpus), "LOADGEN_CPUS": str(r.loadgen_cpus),
            "CODE_PATH": str(self.target.code_path),
            "DOCKERFILE_PATH": str(self.target.compose_file.parent / "Dockerfile"),
        }

def load_manifest(path: str | Path) -> Manifest:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Resolve paths relative to the manifest's own folder, not the cwd.
    target = data.get("target") if isinstance(data, dict) else None
    if isinstance(target, dict):
        for key in ("compose_file", "code_path"):
            if key in target:
                target[key] = str((path.parent / target[key]).resolve())
    return Manifest.model_validate(data)