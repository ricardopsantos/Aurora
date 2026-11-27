"""R494 — the Rust core checkpoints approved mutations into the same shadow
repository and `last-mutation.json` Python does (R47, R181, R193, R273, R275),
so Python's /rewind and /undo work on what the Rust core changed."""

import base64
import json
import subprocess
import sys

from tests.test_rust_compat_r482 import ROOT, _Model, _run, _setup, _sse, _write_call, aurora_rs  # noqa: F401


def _cmd_call(command):
    args = json.dumps({"command": command})
    return _sse({"tool_calls": [{"index": 0, "id": "call_c", "type": "function",
                                 "function": {"name": "run_command", "arguments": args}}]})


def _go(argv, tmp_path, replies, task):
    model = _Model(replies)
    try:
        home, work, _cfg, env = _setup(tmp_path, model.url)
        env["PYTHONPATH"] = str(ROOT)
        (work / "note.txt").write_bytes(b"old \xff bytes\n")     # not UTF-8 (R193)
        (work / "keep.txt").write_text("k\n", encoding="utf-8")
        p = _run(argv + ["-t", task, "--yes"], env, work)
    finally:
        model.close()
    assert p.returncode == 0, p.stderr
    return home, work


def _shadow(home):
    repos = [d for d in (home / "checkpoints").iterdir() if d.is_dir()]
    assert len(repos) == 1, repos
    return repos[0]


def _log(gd, work):
    r = subprocess.run(["git", "--git-dir", str(gd), "--work-tree", str(work), "log", "--format=%s"],
                       capture_output=True, text=True, cwd=work)
    return r.stdout.splitlines()


def _tree(gd, work):
    r = subprocess.run(["git", "--git-dir", str(gd), "--work-tree", str(work), "ls-tree", "-r", "--name-only", "HEAD"],
                       capture_output=True, text=True, cwd=work)
    return r.stdout.splitlines()


def test_same_checkpoints_and_snapshot_as_python(aurora_rs, tmp_path):
    got = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        home, work = _go(argv, tmp_path / name, [_write_call("note.txt", "new\n"), _sse({"content": "ok"})], "rewrite the note")
        gd = _shadow(home)
        assert gd.name == __import__("hashlib").sha1(str(work.resolve()).encode()).hexdigest()[:16]
        m = json.loads((gd / "last-mutation.json").read_text())
        assert m["path"] == str((work / "note.txt").resolve()) and m["existed"] is True
        assert base64.b64decode(m["content_b64"]) == b"old \xff bytes\n"
        assert (gd / "aurora-cwd").read_text() == str(work.resolve())
        got[name] = (_log(gd, work), _tree(gd, work), (gd / "info" / "exclude").read_text(), set(m))
    assert got["rust"] == got["python"]
    assert got["rust"][0] == ["[write_file] rewrite the note"]


def test_python_undo_reverts_a_rust_write(aurora_rs, tmp_path, monkeypatch):
    home, work = _go([aurora_rs], tmp_path, [_write_call("note.txt", "new\n"), _sse({"content": "ok"})], "rewrite")
    assert (work / "note.txt").read_text() == "new\n"
    monkeypatch.setenv("AURORA_HOME", str(home))
    from aurora import rewind
    labels = [e["label"] for e in rewind.entries(str(work))]
    assert labels == ["[write_file] rewrite"]
    out = rewind.undo(str(work))
    assert (work / "note.txt").read_bytes() == b"old \xff bytes\n", out


def test_a_command_clears_the_file_snapshot(aurora_rs, tmp_path):
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        home, work = _go(argv, tmp_path / name, [_write_call("note.txt", "new\n"), _cmd_call("echo hi > made.txt"),
                                                 _sse({"content": "ok"})], "two steps")
        gd = _shadow(home)
        assert not (gd / "last-mutation.json").exists(), name
        assert _log(gd, work) == ["[run_command] two steps", "[write_file] two steps"], name
