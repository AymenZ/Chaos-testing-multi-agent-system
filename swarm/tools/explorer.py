# swarm/tools/explorer.py
"""Read-only window onto the target's source for the planner. No shell, no writes.

Enforced here, in code, not in the prompt: everything is confined to the code root, secrets files
are unreadable, and only source-like file types are allowed (an unexpected type is refused by
default). Every refusal goes to the audit log, so nothing blocked ever reaches the model or a trace.
"""
import fnmatch
import os
import re
from pathlib import Path

from swarm.audit import AuditLog

ALLOWED_SUFFIXES = {".py", ".md", ".txt", ".toml", ".ini", ".cfg", ".yml", ".yaml", ".json", ".sql", ".mako", ".rst"}
ALLOWED_NAMES = {"dockerfile", "makefile", "procfile"}
DENIED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".idea", ".vscode",
               ".pytest_cache", ".mypy_cache"}
DENIED_SUFFIXES = {".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".pyc"}
DENIED_NAMES = {".netrc", "credentials", "credentials.json", "id_rsa", "id_ed25519"}


class JailError(Exception):
    """A refused or invalid request. The message is safe to show the model."""


def _name_denied(name: str) -> bool:
    low = name.lower()
    return low.startswith(".env") or low in DENIED_NAMES or Path(low).suffix in DENIED_SUFFIXES


class CodeJail:
    MAX_READ_LINES = 250
    MAX_CHARS = 20_000
    MAX_TREE = 300
    MAX_GREP = 40
    MAX_FILE_BYTES = 400_000

    def __init__(self, root: Path, audit: AuditLog):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"code root does not exist: {self.root}")
        self.audit = audit

    # --- the gate ----------------------------------------------------------
    def _deny(self, rel: str, reason: str) -> JailError:
        self.audit.event("read_denied", path=rel, reason=reason)
        return JailError(f"{reason}: {rel}")

    def resolve(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise JailError("path must be a non-empty string")
        p = Path(rel)
        if p.is_absolute() or p.drive:
            raise self._deny(rel, "absolute paths are not allowed, use a path relative to the code root")
        full = (self.root / p).resolve()          # follows symlinks, so a link can't smuggle us out
        try:
            parts = full.relative_to(self.root).parts
        except ValueError:
            raise self._deny(rel, "path escapes the code root") from None
        for part in parts:
            if part.lower() in DENIED_DIRS or _name_denied(part):
                raise self._deny(rel, "access to this location is blocked")
        return full

    def _file(self, rel: str) -> Path:
        full = self.resolve(rel)
        if not full.is_file():
            raise JailError(f"not a file: {rel}")
        if full.suffix.lower() not in ALLOWED_SUFFIXES and full.name.lower() not in ALLOWED_NAMES:
            raise self._deny(rel, "file type is not readable")
        if full.stat().st_size > self.MAX_FILE_BYTES:
            raise JailError(f"file too large to read: {rel}")
        return full

    def _rel(self, full: Path) -> str:
        return full.relative_to(self.root).as_posix()

    def _inside(self, p: Path) -> bool:
        """True only if p, after following every link and junction, is still under the root."""
        try:
            p.resolve().relative_to(self.root)
            return True
        except ValueError:
            self.audit.event("read_denied", path=str(p), reason="link leaves the code root")
            return False

    def _walk(self):
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs
                             if d.lower() not in DENIED_DIRS and not _name_denied(d)
                             and self._inside(Path(dirpath) / d))
            for f in sorted(files):
                full = Path(dirpath) / f
                if _name_denied(f) or not self._inside(full):
                    continue
                if full.suffix.lower() in ALLOWED_SUFFIXES or f.lower() in ALLOWED_NAMES:
                    yield full

    # --- tools -------------------------------------------------------------
    def raw_text(self, rel: str) -> str:
        """Whole file text, same gate. Used by checks.py to verify quotes."""
        return self._file(rel).read_text(encoding="utf-8", errors="replace")

    def tree(self) -> str:
        files = list(self._walk())
        lines = [f"{self._rel(f)}  ({f.stat().st_size} B)" for f in files[: self.MAX_TREE]]
        if len(files) > self.MAX_TREE:
            lines.append(f"... {len(files) - self.MAX_TREE} more files not shown; use search_code")
        return "\n".join(lines) or "(no readable files)"

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        text = self.raw_text(path)
        lines = text.splitlines()
        if not isinstance(start, int) or start < 1:
            raise JailError("start must be an integer >= 1")
        if start > max(len(lines), 1):
            raise JailError(f"start {start} is past the end of the file ({len(lines)} lines)")
        stop = min(end if isinstance(end, int) else start + self.MAX_READ_LINES - 1,
                   start + self.MAX_READ_LINES - 1, len(lines))
        body = "\n".join(f"{n:>4}| {lines[n - 1]}" for n in range(start, stop + 1))
        if len(body) > self.MAX_CHARS:
            body = body[: self.MAX_CHARS] + "\n... (output cut; request a smaller range)"
        more = f"\n(continues: call again with start={stop + 1})" if stop < len(lines) else ""
        return f"{path} lines {start}-{stop} of {len(lines)}\n{body}{more}"

    def grep(self, pattern: str, glob: str = "*", ignore_case: bool = True) -> str:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 200:
            raise JailError("pattern must be a regex of 1-200 characters")
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            raise JailError(f"invalid regex: {e}") from None
        hits, total = [], 0
        for f in self._walk():
            rel = self._rel(f)
            if not fnmatch.fnmatch(rel, glob) or f.stat().st_size > self.MAX_FILE_BYTES:
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    total += 1
                    if len(hits) < self.MAX_GREP:
                        hits.append(f"{rel}:{n}: {line.strip()[:200]}")
        if not hits:
            return "no matches"
        return "\n".join(hits) + (f"\n... {total - len(hits)} more matches omitted" if total > len(hits) else "")
