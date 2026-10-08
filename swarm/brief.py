# swarm/brief.py
"""The planner's contract: what the model writes (PlannerOutput) and what the rest of the graph
receives (Brief = that output + facts attached by code + whatever the checks dropped)."""
from enum import Enum
from typing import Any, Literal

from pydantic import Field

from swarm.manifest import Limits, Manifest, RuleSet, Strict


class Signal(str, Enum):
    """What the runner can measure. The planner may only promise evidence from this list."""
    REQUEST_LATENCY_P95 = "request_latency_p95"
    ERROR_RATE = "error_rate"
    ACHIEVED_REQUEST_RATE = "achieved_request_rate"
    API_CPU = "api_cpu"
    DB_CPU = "db_cpu"
    DB_CONNECTIONS = "db_connections"


class Evidence(Strict):
    """A claim's receipt. For kind='code' the quote must literally appear in the file (and inside
    the cited lines, if given); checks.py verifies it. 'openapi' and 'db_schema' refs must exist."""
    kind: Literal["code", "openapi", "db_schema"]
    ref: str = Field(description="file path relative to the code root | 'METHOD /path' | table name")
    quote: str = Field(min_length=3, max_length=300, description="short verbatim excerpt")
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)


class Recipe(Strict):
    """How to make one valid request. Credentials are placeholders filled in by code at run time."""
    auth_role: Literal["none", "user", "admin"]
    query: dict[str, str | int | float | bool] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    form_body: dict[str, str] | None = None


class EndpointNote(Strict):
    key: str = Field(description="'METHOD /path' exactly as listed by get_api_summary")
    load_suitability: Literal["good", "caution", "avoid", "needs_data"]
    writes_data: bool
    cost_notes: str = Field(description="what one request costs the backend, as far as the code shows")
    recipe: Recipe | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class Risk(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,48}$")
    statement: str = Field(min_length=15, description="why the backend might degrade under heavy traffic")
    endpoints: list[str] = Field(min_length=1)
    signals: list[Signal] = Field(min_length=1, description="what would be observable if it is true")
    confidence: Literal["low", "medium", "high"]
    evidence: list[Evidence] = Field(min_length=1)


class PlannerOutput(Strict):
    summary: str = Field(min_length=20)
    endpoints: list[EndpointNote] = Field(min_length=1)
    baseline_targets: list[str] = Field(min_length=1, description="1-2 endpoint keys: safe, representative")
    risks: list[Risk]
    unknowns: list[str] = Field(description="what could not be determined from the code and facts")


class ManifestContext(Strict):
    """Attached by code, never written by the model, so limits cannot be misquoted. No passwords."""
    base_url: str
    login_path: str
    login_encoding: str
    token_field: str
    roles: list[str]
    seed_rows: dict[str, int]
    limits: Limits
    rules: RuleSet

    @classmethod
    def from_manifest(cls, m: Manifest) -> "ManifestContext":
        return cls(
            base_url=str(m.target.base_url), login_path=m.auth.login_path,
            login_encoding=m.auth.login_encoding, token_field=m.auth.token_field,
            roles=sorted({u.role for u in m.auth.seed_users}), seed_rows=m.seed.rows,
            limits=m.limits, rules=m.rules.default,
        )


class Brief(Strict):
    output: PlannerOutput
    context: ManifestContext
    dropped: list[str] = Field(default_factory=list)   # what the checks removed, and why
