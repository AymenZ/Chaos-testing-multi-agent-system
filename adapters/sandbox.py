# adapters/sandbox.py
"""Thin wrapper over `docker compose`. Knows about containers, not about the manifest or the graph.

The stack is on an internal network with no published ports, so every probe runs *inside* the
api container (python is already there), and nothing on the host talks to the target directly.
"""
import json
import os
import subprocess
import time
from pathlib import Path

# These match the db service in the compose file.
DB_USER = "sandbox"
DB_NAME = "app"
MASTER_DB = "app_master"      # the clean seeded copy that reset() clones from

# Runs inside the api container. Prints one JSON line; status 0 means "no response".
_REQUEST = """
import json, sys, urllib.error, urllib.parse, urllib.request
method, url, kind, payload = sys.argv[1:5]
body, headers = None, {}
if kind == "form":
    body = urllib.parse.urlencode(json.loads(payload)).encode()
    headers["Content-Type"] = "application/x-www-form-urlencoded"
elif kind == "json":
    body = payload.encode()
    headers["Content-Type"] = "application/json"
req = urllib.request.Request(url, data=body, headers=headers, method=method)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        status, text = r.status, r.read().decode()
except urllib.error.HTTPError as e:
    status, text = e.code, e.read().decode()
except Exception as e:
    status, text = 0, str(e)
print(json.dumps({"status": status, "body": text}))
"""

_INTERNET = """
import socket
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    print("open")
except OSError:
    print("blocked")
"""


class SandboxError(Exception):
    pass


class Sandbox:
    def __init__(self, compose_file: Path, api_service: str = "api", db_service: str = "db",
                 project: str = "chaos_target", env: dict[str, str] | None = None):
        self.api = api_service
        self.db = db_service
        self._base = ["docker", "compose", "-p", project, "-f", str(compose_file)]
        self._env = {**os.environ, **(env or {})}     # e.g. the cpu limits from the manifest

    def _run(self, *args: str, timeout: int = 120, stdin: str | None = None) -> str:
        try:
            r = subprocess.run([*self._base, *args], capture_output=True, text=True,
                               encoding="utf-8", timeout=timeout, input=stdin, env=self._env)
        except subprocess.TimeoutExpired:
            raise SandboxError(f"'{' '.join(args)}' timed out after {timeout}s")
        if r.returncode != 0:
            raise SandboxError(f"'{' '.join(args)}' failed: {r.stderr.strip()[-500:]}")
        return r.stdout

    # --- lifecycle -------------------------------------------------------
    def up(self) -> None:
        self._run("up", "-d", "--build", timeout=600)

    def down(self) -> None:
        self._run("down", "-v", "--remove-orphans")

    # --- running things inside --------------------------------------------
    def exec(self, service: str, *cmd: str, timeout: int = 300, stdin: str | None = None) -> str:
        return self._run("exec", "-T", service, *cmd, timeout=timeout, stdin=stdin)

    def python(self, script: str, *args: str, timeout: int = 60) -> str:
        """Run a script inside the api container (its working dir is /app, so `import app` works)."""
        return self.exec(self.api, "python", "-", *args, timeout=timeout, stdin=script)

    def psql(self, sql: str, *, db: str = DB_NAME) -> str:
        return self.exec(self.db, "psql", "-U", DB_USER, "-d", db,
                         "-v", "ON_ERROR_STOP=1", "-tA", "-c", sql)

    def request(self, method: str, url: str, *, form: dict | None = None,
                json_body: dict | None = None) -> tuple[int, str]:
        """HTTP call made from inside the sandbox. Returns (status, body); status 0 = no response."""
        kind, payload = ("form", form) if form is not None else \
                        ("json", json_body) if json_body is not None else ("none", None)
        out = self.python(_REQUEST, method, url, kind, json.dumps(payload))
        try:
            reply = json.loads(out.strip().splitlines()[-1])
            return int(reply["status"]), str(reply["body"])
        except (IndexError, ValueError, KeyError) as e:
            raise SandboxError(f"unreadable reply from in-sandbox request: {out[-200:]!r}") from e

    def has_internet(self) -> bool:
        return self.python(_INTERNET).strip() == "open"

    def wait_healthy(self, url: str, timeout_s: int = 60) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.request("GET", url)[0] == 200:
                    return
            except SandboxError:
                pass                      # container not up yet
            time.sleep(1)
        raise SandboxError(f"{url} not healthy after {timeout_s}s")

    # --- clean-state copy ---------------------------------------------------
    def _swap_db(self, statements: list[str], health_url: str) -> None:
        """Stop the api (so no connections remain), run DB statements, start it, wait for health."""
        self._run("stop", self.api)
        for sql in statements:
            self.psql(sql, db="postgres")
        self._run("start", self.api)
        self.wait_healthy(health_url)

    def snapshot(self, health_url: str) -> None:
        """Save the current (seeded) database as the clean copy."""
        self._swap_db([f"CREATE DATABASE {MASTER_DB} TEMPLATE {DB_NAME}"], health_url)

    def reset(self, health_url: str) -> None:
        """Back to the clean copy in seconds: no rebuild, no reseed."""
        self._swap_db([f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)",
                       f"CREATE DATABASE {DB_NAME} TEMPLATE {MASTER_DB}"], health_url)
