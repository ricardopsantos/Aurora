"""R489 (also R269: CRLF edits) — the Rust `grep` and `find_files` answer exactly as `aurora/tools.py`
does, on the same tree and the same arguments (Python is the oracle)."""

import json
import os
import subprocess

from aurora import tools
from tests.test_rust_compat_r482 import aurora_rs  # noqa: F401


def _rs(aurora_rs, calls, cwd):
    p = subprocess.run([aurora_rs, "--tool-oracle"], input=json.dumps(calls), cwd=cwd,
                       capture_output=True, text=True, timeout=120,
                       env={k: v for k, v in os.environ.items() if not k.startswith("AURORA_")})
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _tree(root):
    files = {
        "a.py": "import os\nprint('olá')\n",
        "src/b.py": "def foo(bar):\n    return bar + 1\n",
        "src/deep/c.txt": "foo(bar) here\nTODO: x\n",
        "src/name with space.md": "# title\nfoo\n",
        "node_modules/x.py": "foo\n",
        ".git/config": "foo\n",
        "build/out.py": "foo\n",
        "tests/test_a.py": "def test_foo():\n    assert True\n",
        "bin.dat": None,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            p.write_bytes(b"\x00\x01foo\x00")
        else:
            p.write_text(text, encoding="utf-8")
    (root / "link_to_src").symlink_to(root / "src")


CALLS = [
    ("find_files", {"pattern": "*.py"}),
    ("find_files", {"pattern": "*.py", "path": "src"}),
    ("find_files", {"pattern": "src/*"}),
    ("find_files", {"pattern": "tests/test_*.py", "path": "."}),
    ("find_files", {"pattern": "*.[mt][dx]*"}),
    ("find_files", {"pattern": "*.rs"}),
    ("find_files", {"pattern": "*", "path": "nope"}),
    ("grep", {"pattern": "foo"}),
    ("grep", {"pattern": "foo\\(bar|TODO", "path": "src"}),
    ("grep", {"pattern": "def (foo|test_foo)", "path": "./"}),
    ("grep", {"pattern": "olá", "path": "a.py"}),
    ("grep", {"pattern": "zzz"}),
    ("grep", {"pattern": "(", "path": "."}),
    ("grep", {"pattern": "x", "path": "missing-dir"}),
]


def _norm(tool, out):
    # grep's own line order follows readdir; compare as sets of lines
    return sorted(out.splitlines()) if tool == "grep" else out


def test_search_tools_match_python(aurora_rs, tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    rs = _rs(aurora_rs, [{"tool": t, "args": a} for t, a in CALLS], tmp_path)
    for (tool, args), got in zip(CALLS, rs):
        want = getattr(tools, tool)(**args)
        assert _norm(tool, got) == _norm(tool, want), (tool, args, want, got)


# R490: apply_patch and edit_file's R309/R457 paths, same file, same arguments
SRC = "def f():\n    if x:\n        return 1\n---\ntotal_count = 5\ncount = 5\n\tlast\n"
EDITS = [
    ("apply_patch", {"diff": "--- a/s.py\n+++ b/s.py\n@@ -5,2 +5,2 @@\n total_count = 5\n-count = 5\n+count = 6\n"}),
    ("apply_patch", {"diff": "@@ -1 +1 @@\n-otal_count = 5\n+x\n"}),
    ("apply_patch", {"diff": "@@ -4 +4 @@\n----\n+===\n"}),
    ("apply_patch", {"diff": "@@ -1 +1 @@\n+only added\n"}),
    ("apply_patch", {"diff": "@@ x @@\n"}),
    ("apply_patch", {"diff": "no hunks here"}),
    ("apply_patch", {"diff": "@@ -1 +1 @@\n count = 5\n"}),
    ("apply_patch", {"diff": "@@ -1 +1 @@\n-    return 1\n+    return 2\n"}),
    ("apply_patch", {"diff": "@@ -1 +1 @@\n if x:\n*bad\n"}),
    ("apply_patch", {"diff": "@@ -1 +1 @@\n-  if x:\n+  if y:\n"}),
    ("apply_patch", {"diff": "@@ -1,2 +1,2 @@\n def f():\n-    if z:\n+    if y:\n"}),
    ("edit_file", {"old": "if x:\n    return 1", "new": "if y:\n    return 2"}),
    ("edit_file", {"old": "count  =  5\n\tlast", "new": "count = 7"}),
    ("edit_file", {"old": "  total_count = 5  ", "new": "t"}),
    ("edit_file", {"old": "def f():\n    if q:", "new": "t"}),
    ("edit_file", {"old": "nowhere", "new": "t"}),
    ("edit_file", {"old": "count = 5", "new": "c"}),
]


def test_patch_and_edit_match_python(aurora_rs, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for crlf in (False, True):
        src = SRC.replace("\n", "\r\n") if crlf else SRC
        for tool, args in EDITS:
            outs = {}
            for side in ("py", "rs"):
                f = tmp_path / f"{side}.py"
                f.write_bytes(src.encode())
                a = dict(args, path=f.name)
                if side == "py":
                    res = getattr(tools, tool)(**a)
                else:
                    res = _rs(aurora_rs, [{"tool": tool, "args": a}], tmp_path)[0]
                outs[side] = (res.replace(f.name, "F"), f.read_bytes())
            assert outs["rs"] == outs["py"], (crlf, tool, args, outs)


# R491: wait_until, same commands, timing normalised
WAITS = [
    {"command": "true"},
    {"command": "echo up"},
    {"command": "n=$(cat n 2>/dev/null || echo 0); echo $((n+1)) > n; [ $n -ge 2 ]", "interval": 0.5, "timeout": 10},
    {"command": "echo not yet; exit 3", "interval": 0.5, "timeout": 1.2},
    {"command": "true", "then": "echo done; exit 4"},
    {"command": "true", "then": "true"},
    {"command": "true", "cwd": "no/such/dir"},
    {"command": "pwd", "cwd": "sub"},
    {"command": "true", "timeout": "abc"},
]


def test_wait_until_matches_python(aurora_rs, tmp_path, monkeypatch):
    import re
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    norm = lambda s: re.sub(r"\d+\.\d+s\]", "Ns]", s)  # noqa: E731
    for args in WAITS:
        (tmp_path / "n").unlink(missing_ok=True)
        py = tools.run_tool("wait_until", args)
        (tmp_path / "n").unlink(missing_ok=True)
        rs = _rs(aurora_rs, [{"tool": "wait_until", "args": args}], tmp_path)[0]
        assert norm(rs) == norm(py), (args, py, rs)
