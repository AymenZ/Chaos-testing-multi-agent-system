# swarm/tools/dbschema.py
"""Deterministic database facts: tables, columns, indexes and row counts. No LLM involved."""
import json

from adapters.sandbox import Sandbox

_COLUMNS = ("SELECT json_agg(t) FROM (SELECT table_name, column_name, data_type, udt_name, is_nullable, "
            "column_default FROM information_schema.columns WHERE table_schema = 'public' "
            "ORDER BY table_name, ordinal_position) t")
_INDEXES = ("SELECT json_agg(t) FROM (SELECT tablename AS table_name, indexname, indexdef "
            "FROM pg_indexes WHERE schemaname = 'public' ORDER BY tablename, indexname) t")


def _rows(sandbox: Sandbox, sql: str) -> list[dict]:
    out = sandbox.psql(sql).strip()
    return json.loads(out) if out else []


def read_db_schema(sandbox: Sandbox) -> dict:
    tables: dict[str, dict] = {}
    for c in _rows(sandbox, _COLUMNS):
        t = tables.setdefault(c["table_name"], {"columns": [], "indexes": [], "rows": None})
        t["columns"].append({"name": c["column_name"], "type": c["udt_name"] if c["data_type"] == "USER-DEFINED" else c["data_type"],
                             "nullable": c["is_nullable"] == "YES", "default": c["column_default"]})
    for i in _rows(sandbox, _INDEXES):
        tables.setdefault(i["table_name"], {"columns": [], "indexes": [], "rows": None})["indexes"].append(i["indexdef"])
    for name, t in tables.items():
        quoted = '"' + name.replace('"', '""') + '"'      # names come from the catalog, quoted anyway
        t["rows"] = int(sandbox.psql(f"SELECT count(*) FROM {quoted}").strip())
    return {"tables": tables}
