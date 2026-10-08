# swarm/checks.py
"""Mechanical verification of the planner's output against deterministic facts. The model's claims
are kept only if they check out. Dropped items are logged and listed on the brief; if too little
survives, `blocking` explains why so the planner can be told to try again within its budget."""
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
    blocking: list[str] = field(default_factory=list)    # essentials missing: the brief is unusable


def _norm(s: str) -> str:
    return " ".join(s.split())


def evidence_problem(ev: Evidence, jail: CodeJail, endpoints: dict, tables: dict) -> str | None:
    """None means the receipt checks out. Code quotes must literally appear in the cited file/lines."""
    if ev.kind == "openapi":
        return None if ev.ref in endpoints else f"unknown endpoint '{ev.ref}'"
    if ev.kind == "db_schema":
        return None if ev.ref in tables else f"unknown table '{ev.ref}'"
    try:
        text = jail.raw_text(ev.ref)
    except JailError as e:
        return f"cannot read '{ev.ref}': {e}"
    if ev.line_start is not None:
        lines = text.splitlines()
        end = ev.line_end or ev.line_start
        if ev.line_start > len(lines) or end < ev.line_start:
            return f"line range {ev.line_start}-{end} is outside {ev.ref} ({len(lines)} lines)"
        text = " ".join(lines[ev.line_start - 1:end])
    return None if _norm(ev.quote) in _norm(text) else f"quote not found in {ev.ref}"


def _recipe_problem(recipe: Recipe, entry: dict) -> str | None:
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


def check_output(out: PlannerOutput, summary: dict, db_schema: dict, jail: CodeJail, audit: AuditLog) -> CheckResult:
    endpoints = index_by_key(summary)
    tables = db_schema.get("tables", {})
    dropped: list[str] = []

    def drop(msg: str) -> None:
        dropped.append(msg)
        audit.event("check_dropped", reason=msg)

    def keep_evidence(items: list[Evidence], owner: str) -> list[Evidence]:
        kept = []
        for ev in items:
            problem = evidence_problem(ev, jail, endpoints, tables)
            if problem:
                drop(f"{owner}: evidence dropped ({problem})")
            else:
                kept.append(ev)
        return kept

    notes, seen = [], set()
    for note in out.endpoints:
        entry = endpoints.get(note.key)
        if entry is None:
            drop(f"endpoint '{note.key}' is not in the API summary")
            continue
        if note.key in seen:
            drop(f"endpoint '{note.key}' listed twice")
            continue
        seen.add(note.key)
        update: dict = {"evidence": keep_evidence(note.evidence, note.key)}
        if entry["path_params"] and note.load_suitability == "good":
            update.update(load_suitability="needs_data", recipe=None)
            drop(f"{note.key}: needs identifiers we cannot supply, marked needs_data")
        elif note.recipe is not None:
            problem = _recipe_problem(note.recipe, entry)
            if problem:
                update["recipe"] = None
                drop(f"{note.key}: recipe dropped ({problem})")
                if note.load_suitability == "good":
                    update["load_suitability"] = "needs_data"
        elif note.load_suitability == "good":
            update["load_suitability"] = "needs_data"
            drop(f"{note.key}: marked good but has no recipe, marked needs_data")
        notes.append(note.model_copy(update=update))

    risks = []
    for risk in out.risks:
        known = [k for k in risk.endpoints if k in endpoints]
        if len(known) < len(risk.endpoints):
            drop(f"risk '{risk.id}': unknown endpoints removed")
        evidence = keep_evidence(risk.evidence, f"risk '{risk.id}'")
        if not known or not evidence:
            drop(f"risk '{risk.id}' dropped: no valid endpoints or evidence left")
            continue
        risks.append(risk.model_copy(update={"endpoints": known, "evidence": evidence}))

    usable = {n.key for n in notes if n.load_suitability == "good" and n.recipe is not None}
    targets = [t for t in dict.fromkeys(out.baseline_targets) if t in usable]
    if len(targets) < len(out.baseline_targets):
        drop("baseline targets removed: not good-and-runnable endpoints")
    blocking = []
    if not notes:
        blocking.append("no endpoint notes survived the checks")
    if not targets:
        blocking.append("no baseline target survives: need at least one endpoint marked good with a valid recipe")

    cleaned = out.model_copy(update={"endpoints": notes, "risks": risks, "baseline_targets": targets})
    return CheckResult(cleaned, dropped, blocking)
