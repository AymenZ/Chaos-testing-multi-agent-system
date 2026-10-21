# swarm/checks.py
"""Mechanical verification of the planner's output. Kept deliberately small: it checks only what
would silently corrupt something downstream (invented endpoints, unsupported claims, unusable
baseline requests). Dropped items are logged and listed on the brief; `blocking` explains why a
brief is unusable so the planner can be told to try again within its budget."""
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator

from swarm.audit import AuditLog
from swarm.brief import Evidence, PlannerOutput, Recipe
from swarm.tools.explorer import CodeJail, JailError
from swarm.tools.openapi import index_by_key


@dataclass
class CheckResult:
    output: PlannerOutput
    dropped: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return " ".join(s.split())


def evidence_problem(ev: Evidence, jail: CodeJail) -> str | None:
    """None means the receipt checks out: the quote literally appears in the cited file."""
    try:
        text = jail.raw_text(ev.ref)
    except JailError as e:
        return f"cannot read '{ev.ref}': {e}"
    return None if _norm(ev.quote) in _norm(text) else f"quote not found in {ev.ref}"


def recipe_problem(recipe: Recipe, entry: dict) -> str | None:
    """Would this request actually be accepted? A bad recipe makes every call an error."""
    if entry["auth_required"] and recipe.auth_role == "none":
        return "endpoint requires auth but the recipe sends none (every call would be a 401)"
    known = {q["name"] for q in entry["query_params"]}
    if set(recipe.query) - known:
        return f"unknown query parameters {sorted(set(recipe.query) - known)}"
    missing = [q["name"] for q in entry["query_params"] if q["required"] and q["name"] not in recipe.query]
    if missing:
        return f"missing required query parameters {missing}"
    body = entry["request_body"]
    given = recipe.json_body if recipe.json_body is not None else recipe.form_body
    if body is None:
        return "recipe has a body but the endpoint takes none" if given is not None else None
    if given is None:
        return "endpoint needs a request body and the recipe has none"
    if ("json" in body["content_type"]) != (recipe.json_body is not None):
        return f"endpoint expects {body['content_type']}"
    errors = sorted(Draft202012Validator(body["schema"]).iter_errors(given), key=lambda e: list(e.path))
    return f"body does not match the schema: {errors[0].message}" if errors else None


def check_output(out: PlannerOutput, summary: dict, jail: CodeJail, audit: AuditLog) -> CheckResult:
    endpoints = index_by_key(summary)
    dropped: list[str] = []

    def drop(msg: str) -> None:
        dropped.append(msg)
        audit.event("check_dropped", reason=msg)

    def keep_evidence(items: list[Evidence], owner: str) -> list[Evidence]:
        kept = []
        for ev in items:
            problem = evidence_problem(ev, jail)
            if problem:
                drop(f"{owner}: evidence dropped ({problem})")
            else:
                kept.append(ev)
        return kept

    notes = []
    for note in out.endpoints:
        entry = endpoints.get(note.key)
        if entry is None:
            drop(f"endpoint '{note.key}' is not in the API summary")
            continue
        update: dict = {"evidence": keep_evidence(note.evidence, note.key)}
        problem = recipe_problem(note.recipe, entry) if note.recipe else None
        if problem:
            update["recipe"] = None
            drop(f"{note.key}: recipe dropped ({problem})")
        notes.append(note.model_copy(update=update))

    risks = []
    for risk in out.risks:
        evidence = keep_evidence(risk.evidence, f"risk '{risk.id}'")
        if not evidence or any(k not in endpoints for k in risk.endpoints):
            drop(f"risk '{risk.id}' dropped: unknown endpoint or no valid evidence")
            continue
        risks.append(risk.model_copy(update={"evidence": evidence}))

    # A baseline request must be runnable with no data we don't have: good, a valid recipe, no path ids.
    usable = {n.key for n in notes
              if n.load_suitability == "good" and n.recipe is not None and not endpoints[n.key]["path_params"]}
    targets = [t for t in dict.fromkeys(out.baseline_targets) if t in usable]
    if len(targets) < len(out.baseline_targets):
        drop("baseline targets removed: must be 'good', have a valid recipe and need no path identifiers")
    blocking = [] if targets else [
        "no usable baseline target: mark at least one endpoint 'good' with a valid recipe and no path parameters"]

    return CheckResult(out.model_copy(update={"endpoints": notes, "risks": risks, "baseline_targets": targets}),
                       dropped, blocking)
