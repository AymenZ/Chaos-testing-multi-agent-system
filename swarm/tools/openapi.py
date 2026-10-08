# swarm/tools/openapi.py
"""Deterministic OpenAPI summary: the ground truth for what endpoints exist. No LLM involved."""

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _ref(spec: dict, ref: str) -> dict:
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def resolve(spec: dict, node, seen: tuple = (), depth: int = 0):
    """Inline every $ref so a schema is self-contained (cycles and depth are cut off)."""
    if isinstance(node, dict):
        if "$ref" in node:
            if node["$ref"] in seen or depth > 8:
                return {"type": "object", "description": "(recursive schema, cut off)"}
            return resolve(spec, _ref(spec, node["$ref"]), seen + (node["$ref"],), depth + 1)
        return {k: resolve(spec, v, seen, depth) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve(spec, v, seen, depth) for v in node]
    return node


def summarize_openapi(spec: dict) -> dict:
    endpoints = []
    for path, item in spec.get("paths", {}).items():
        shared = item.get("parameters", [])
        for method in HTTP_METHODS:
            op = item.get(method)
            if op is None:
                continue
            params = [resolve(spec, p) for p in [*shared, *op.get("parameters", [])]]
            body = None
            content = (op.get("requestBody") or {}).get("content") or {}
            if content:
                ctype, media = next(iter(content.items()))
                body = {"content_type": ctype, "schema": resolve(spec, media.get("schema", {}))}
            endpoints.append({
                "key": f"{method.upper()} {path}",
                "summary": op.get("summary", ""),
                "auth_required": bool(op.get("security") or spec.get("security")),
                "path_params": [{"name": p["name"], "schema": p.get("schema", {})} for p in params if p.get("in") == "path"],
                "query_params": [{"name": p["name"], "required": p.get("required", False), "schema": p.get("schema", {})}
                                 for p in params if p.get("in") == "query"],
                "request_body": body,
                "response_codes": sorted(op.get("responses", {})),
            })
    return {"title": spec.get("info", {}).get("title", ""), "endpoints": endpoints}


def index_by_key(summary: dict) -> dict[str, dict]:
    return {e["key"]: e for e in summary["endpoints"]}
