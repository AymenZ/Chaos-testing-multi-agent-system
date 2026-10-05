"""Manifest: the input contract and safety envelope for one target."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(_Strict):
    compose_file: Path
    api_service: str = "api"
    db_service: str = "db"
    health_path: str = "/health"
    base_url: str  # as seen from inside the sandbox network
    code_path: Path
    openapi_path: str = "/openapi.json"


class SeedUser(_Strict):
    email: str
    password: str
    role: Literal["user", "admin"] = "user"


class Auth(_Strict):
    register_path: str
    login_path: str
    login_encoding: Literal["form", "json"] = "form"
    username_field: str = "username"
    password_field: str = "password"
    token_field: str = "access_token"
    seed_users: list[SeedUser] = Field(min_length=1)


class SeedPlan(_Strict):
    method: Literal["direct_insert", "api"] = "direct_insert"
    rows: dict[str, int]  # table -> row count; missing indexes only show at volume


class Envelope(_Strict):
    max_vus: int = Field(default=50, ge=1)
    max_duration_s: int = Field(default=120, ge=1)
    allow_destructive: bool = False
    allowed_fault_types: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)  # defaults to base_url host


class SLO(_Strict):
    p95_vs_baseline_max: float = Field(default=2.0, gt=1)
    error_rate_max: float = Field(default=0.01, ge=0, le=1)
    p95_ms_ceiling: float | None = None  # optional absolute ceiling


class AppTests(_Strict):
    test_command: str | None = None


class Resources(_Strict):
    api_cpus: float = 1.0
    db_cpus: float = 1.0
    loadgen_cpus: float = 1.0
    api_mem_mb: int = 512
    db_mem_mb: int = 1024
    loadgen_mem_mb: int = 512
    host_cpu_limit_pct: float = 85.0


class Budgets(_Strict):
    max_experiments: int = 30
    max_attempts: int = 3
    max_followup_rounds: int = 2
    wall_clock_s: int = 3600
    llm_cost_usd: float = 5.0


class Manifest(_Strict):
    target: Target
    auth: Auth
    seed: SeedPlan
    envelope: Envelope = Field(default_factory=Envelope)
    slos: dict[str, SLO]
    app_tests: AppTests = Field(default_factory=AppTests)
    resources: Resources = Field(default_factory=Resources)
    budgets: Budgets = Field(default_factory=Budgets)

    @model_validator(mode="after")
    def _defaults(self) -> "Manifest":
        if "default" not in self.slos:
            raise ValueError("slos must define a 'default' class")
        if not self.envelope.allowed_hosts:
            host = urlparse(self.target.base_url).hostname
            if not host:
                raise ValueError(f"cannot derive host from base_url {self.target.base_url!r}")
            self.envelope.allowed_hosts = [host]
        return self

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
