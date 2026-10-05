"""Experiment spec: the JSON card testers write and the runner executes.

Version 1 is a single endpoint plus auth. Thresholds are deliberately absent:
pass/fail numbers come from the manifest (principle 3), never from the spec.
"""

import hashlib
import json
import operator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Step(_Strict):
    endpoint: str  # path only; the host always comes from the manifest
    method: Literal["GET", "POST", "PATCH", "DELETE"] = "GET"
    auth: Literal["none", "user", "admin"] = "none"
    vus: int = Field(ge=1)
    duration_s: int = Field(ge=1)
    ramp_s: int = Field(default=0, ge=0)
    json_body: dict | None = None
    form_body: dict[str, str] | None = None


class AbortIf(_Strict):
    error_rate_above: float | None = Field(default=0.5, gt=0, le=1)


class ExpectedStimulus(_Strict):
    """What the validator checks to confirm the test ran as designed."""

    min_achieved_vus: int = Field(ge=1)
    min_duration_s: int = Field(ge=1)
    expected_status: list[int] = Field(default_factory=lambda: [200], min_length=1)


_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}


class MechanismSignal(_Strict):
    """Something observable if the hypothesis is really being exercised."""

    metric: str
    op: Literal[">=", "<=", ">", "<"]
    value: float

    def holds(self, observed: float) -> bool:
        return _OPS[self.op](observed, self.value)


class Spec(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    hypothesis: str = Field(min_length=10)
    type: Literal["load"] = "load"  # db and fault arrive in later slices
    destructive: bool = False
    slo_class: str = "default"  # selects a rule set in the manifest
    steps: list[Step] = Field(min_length=1, max_length=1)  # v1: single endpoint
    abort_if: AbortIf = Field(default_factory=AbortIf)
    expected_stimulus: ExpectedStimulus
    mechanism_signals: list[MechanismSignal] = Field(min_length=1)

    @field_validator("steps")
    @classmethod
    def _endpoint_is_path(cls, steps: list[Step]) -> list[Step]:
        for s in steps:
            if not s.endpoint.startswith("/") or "://" in s.endpoint or s.endpoint.startswith("//"):
                raise ValueError(f"endpoint must be a path starting with '/': {s.endpoint!r}")
        return steps

    def param_hash(self) -> str:
        """Identity of the experiment's parameters, ignoring id and prose.

        Used by the scheduler to dedupe and by follow-up rounds to reject repeats.
        """
        body = self.model_dump(mode="json", exclude={"id", "hypothesis"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
