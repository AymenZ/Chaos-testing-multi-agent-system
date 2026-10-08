# tests/test_explorer.py
"""The jail is a safety control, so it gets adversarial tests."""
import os
import subprocess

import pytest

from swarm.audit import AuditLog
from swarm.tools.explorer import CodeJail, JailError


@pytest.fixture
def code(tmp_path):
    root = tmp_path / "code"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("import os\n\ndef health():\n    return 'ok'\n", encoding="utf-8")
    (root / "app" / "db.py").write_text("engine = create_engine(URL)\npool_size = 5\n", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (root / "Dockerfile").write_text("FROM python\n", encoding="utf-8")
    (root / ".env").write_text("SECRET_KEY=hunter2\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "x.py").write_text("junk\n", encoding="utf-8")
    (root / "server.pem").write_text("-----BEGIN-----\n", encoding="utf-8")
    (root / "data.bin").write_bytes(b"\x00\x01")
    (tmp_path / "outside.py").write_text("SECRET = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def jail(code):
    return CodeJail(code, AuditLog())


def test_tree_lists_source_and_hides_secrets_and_noise(jail):
    tree = jail.tree()
    assert "app/main.py" in tree and "Dockerfile" in tree and "requirements.txt" in tree
    for hidden in (".env", ".git", ".venv", "server.pem", "data.bin"):
        assert hidden not in tree


@pytest.mark.parametrize("path", [".env", ".git/config", ".venv/x.py", "server.pem", "data.bin",
                                  "../outside.py", "app/../../outside.py", "app/../.env"])
def test_blocked_paths(jail, path):
    with pytest.raises(JailError):
        jail.read_file(path)


def test_absolute_paths_blocked(jail, tmp_path):
    with pytest.raises(JailError):
        jail.read_file(str(tmp_path / "outside.py"))
    assert "read_denied" in jail.audit.kinds()


def test_denials_are_audited(jail):
    for p in (".env", "../outside.py"):
        with pytest.raises(JailError):
            jail.read_file(p)
    denied = [e for e in jail.audit.events if e["kind"] == "read_denied"]
    assert [e["path"] for e in denied] == [".env", "../outside.py"]


def _link_dir_outside(link, target) -> bool:
    """A symlink, or on Windows without symlink rights, a directory junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        if os.name != "nt":
            return False
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return r.returncode == 0


def test_link_out_of_the_root_is_blocked(code, jail, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "leak.py").write_text("SECRET = 1\n", encoding="utf-8")
    if not _link_dir_outside(code / "app" / "sneaky", outside):
        pytest.skip("cannot create links on this machine")
    with pytest.raises(JailError):
        jail.read_file("app/sneaky/leak.py")
    assert "leak.py" not in jail.tree() and "leak.py" not in jail.grep("SECRET")
    assert "read_denied" in jail.audit.kinds()


def test_read_file_numbers_lines_and_pages(jail):
    out = jail.read_file("app/main.py", 3, 4)
    assert "lines 3-4 of 4" in out and "   3| def health():" in out
    with pytest.raises(JailError):
        jail.read_file("app/main.py", 99)


def test_read_file_caps_page_size(code):
    (code / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(1000)), encoding="utf-8")
    out = CodeJail(code, AuditLog()).read_file("big.py", 1, 1000)
    assert "lines 1-250 of 1000" in out and "continues: call again with start=251" in out


def test_grep_finds_lines_and_never_searches_secrets(jail):
    assert "app/db.py:2: pool_size = 5" in jail.grep("pool_size")
    assert jail.grep("hunter2") == "no matches"
    assert jail.grep("pool", glob="requirements*") == "no matches"
    with pytest.raises(JailError):
        jail.grep("(unclosed")


def test_missing_root_rejected(tmp_path):
    with pytest.raises(ValueError):
        CodeJail(tmp_path / "nope", AuditLog())
