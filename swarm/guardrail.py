"""Spec lint and manifest-envelope checks. Deterministic; holds even if a tester misbehaves."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from swarm.manifest import Manifest
from swarm.spec import Spec


@dataclass
class GuardrailResult:
    accepted: bool
    spec: Spec | None  # the (possibly clamped) spec to run; None when rejected
    violations: list[str] = field(default_factory=list)  # reasons for rejection
    clamped: list[str] = field(default_factory=list)  # adjustments made


def _path_regex(openapi_path: str) -> re.Pattern[str]:
    segments = ("[^/]+" if s.startswith("{") and s.endswith("}") else re.escape(s)
                for s in openapi_path.split("/"))
    return re.compile("^" + "/".join(segments) + "$")


def lint_against_openapi(spec: Spec, openapi: dict) -> list[str]:
    """The endpoint must exist, and its auth requirement must match what the spec sends."""
    errors: list[str] = []
    paths: dict = openapi.get("paths", {})
    for step in spec.steps:
        concrete = step.endpoint.split("?", 1)[0]
        item = next((v for p, v in paths.items() if _path_regex(p).match(concrete)), None)
        if item is None:
            errors.append(f"endpoint {concrete} is not in the OpenAPI spec")
            continue
        op = item.get(step.method.lower())
        if op is None:
            errors.append(f"{step.method} {concrete} is not defined in the OpenAPI spec")
            continue
        if op.get("security") and step.auth == "none":
            errors.append(f"{step.method} {concrete} requires auth but the spec sends none "
                          "(every request would be a 401 and measure nothing)")
    return errors


def check_envelope(spec: Spec, manifest: Manifest, openapi: dict | None = None) -> GuardrailResult:
    """Reject out-of-bounds specs; clamp load and duration down to the envelope."""
    env = manifest.envelope
    violations: list[str] = []
    clamped: list[str] = []

    if spec.slo_class not in manifest.slos:
        violations.append(f"unknown slo_class {spec.slo_class!r}")
    if spec.destructive and not env.allow_destructive:
        violations.append("destructive specs are not allowed by this manifest")

    for step in spec.steps:
        host = urlparse(step.endpoint).hostname
        if host and host not in env.allowed_hosts:  # defence in depth; Spec already forbids hosts
            violations.append(f"host {host!r} is not in the allowlist")

    if openapi is not None:
        violations.extend(lint_against_openapi(spec, openapi))

    if violations:
        return GuardrailResult(False, None, violations)

    fixed = spec.model_copy(deep=True)
    for step in fixed.steps:
        if step.vus > env.max_vus:
            clamped.append(f"vus {step.vus} -> {env.max_vus}")
            step.vus = env.max_vus
        if step.duration_s > env.max_duration_s:
            clamped.append(f"duration_s {step.duration_s} -> {env.max_duration_s}")
            step.duration_s = env.max_duration_s
    # Keep the declared expectations reachable after clamping, or the run could never validate.
    top_vus = max(s.vus for s in fixed.steps)
    top_dur = max(s.duration_s for s in fixed.steps)
    es = fixed.expected_stimulus
    es.min_achieved_vus = min(es.min_achieved_vus, top_vus)
    es.min_duration_s = min(es.min_duration_s, top_dur)
    return GuardrailResult(True, fixed, [], clamped)
