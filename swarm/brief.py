# swarm/brief.py
"""The planner's contract: what the model writes (PlannerOutput) and what the rest of the graph
receives (Brief = that output + facts attached by code + whatever the checks dropped)."""
import re
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from swarm.manifest import Limits, Manifest, RuleSet, Strict


MAX_QUOTE = 200      # characters of a source quote kept as evidence
_KEY = re.compile(r"""^\s*([A-Za-z]+)\s+(/[^\s"',]*)""")


def clean_key(v: Any) -> Any:
    """'GET /a/{id}",load_suitability:' -> 'GET /a/{id}'. Models sometimes leak JSON syntax into a key."""
    m = _KEY.match(v) if isinstance(v, str) else None
    return f"{m.group(1).upper()} {m.group(2)}" if m else v


class Signal(str, Enum):
    """What the runner can measure. The planner may only promise evidence from this list."""
    REQUEST_LATENCY_P95 = "request_latency_p95"
    ERROR_RATE = "error_rate"
    ACHIEVED_REQUEST_RATE = "achieved_request_rate"
    API_CPU = "api_cpu"
    DB_CPU = "db_cpu"
    DB_CONNECTIONS = "db_connections"


class Evidence(Strict):
    """A claim's receipt: a short verbatim quote from a source file. checks.py verifies that the
    quote literally appears in that file, and discards the claim if it does not."""
    ref: str = Field(description="file path relative to the code root")
    quote: str = Field(min_length=3, description="short verbatim excerpt, at most 200 characters")

    @field_validator("quote")
    @classmethod
    def _keep_the_start_of_a_long_quote(cls, v: str) -> str:
        return v[:MAX_QUOTE]                    # a prefix of a verbatim quote is still verbatim


class Recipe(Strict):
    """How to make one valid request. Credentials are placeholders filled in by code at run time."""
    auth_role: Literal["none", "user", "admin"]
    query: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("query", mode="before")
    @classmethod
    def _null_query_is_empty(cls, v: Any) -> Any:
        return {} if v is None else v           # models write null for "no query parameters"

    json_body: dict[str, Any] | None = None
    form_body: dict[str, str] | None = None


class EndpointNote(Strict):
    key: str = Field(description="'METHOD /path' exactly as listed by get_api_summary")
    load_suitability: Literal["good", "caution", "avoid", "needs_data"]
    writes_data: bool = Field(default=False, description="true if a request changes stored data")
    cost_notes: str = Field(description="what one request costs the backend, as far as the code shows")
    recipe: Recipe | None = None
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _infer_missing_writes_data(cls, data: Any) -> Any:
        """Repair a key with leaked syntax; when writes_data is missing, assume the safe answer for the method."""
        if isinstance(data, dict):
            data = {**data, "key": clean_key(data.get("key"))}
            if data.get("writes_data") is None:
                method = str(data["key"]).split(" ", 1)[0].upper()
                data["writes_data"] = method not in ("GET", "HEAD", "OPTIONS")
        return data


class Risk(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,48}$")
    statement: str = Field(min_length=15, description="why the backend might degrade under heavy traffic")
    endpoints: list[str] = Field(min_length=1)

    @field_validator("endpoints", mode="before")
    @classmethod
    def _clean_endpoint_keys(cls, v: Any) -> Any:
        return [clean_key(k) for k in v] if isinstance(v, list) else v

    signals: list[Signal] = Field(min_length=1, description="what would be observable if it is true")
    confidence: Literal["low", "medium", "high"]
    evidence: list[Evidence] = Field(min_length=1)


class PlannerOutput(Strict):
    summary: str = Field(min_length=20)
    endpoints: list[EndpointNote] = Field(min_length=1)
    baseline_targets: list[str] = Field(min_length=1, description="1-2 endpoint keys: safe, representative")
    risks: list[Risk]
    unknowns: list[str] = Field(description="what could not be determined from the code and facts")

    @field_validator("baseline_targets", mode="before")
    @classmethod
    def _targets_as_keys(cls, v: Any) -> Any:
        """Accept {'method': 'GET', 'path': '/x'} (or a 'key') as well as 'GET /x'."""
        if not isinstance(v, list):
            return v
        out = []
        for t in v:
            if isinstance(t, dict):
                t = t.get("key") or t.get("endpoint") or f"{t.get('method', '')} {t.get('path', '')}".strip()
            out.append(clean_key(t))
        return out


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
