"""R499 — Python's front ends on the Rust core: with `runtime.core: rust`,
`Engine.send` runs the turn's loop in `aurora-rs --core` (aurora-core/1) and
everything else in Python. The same task through Python's own headless front
end must give the same session records, the same requests and the same files
on either core."""

import json
import sys

from tests.test_rust_compat_r482 import (_Model, _records, _run, _setup, _sse, _tool_call,  # noqa: F401
                                         _write_call, aurora_rs)

KEYS = ("event", "text", "model", "tool", "decision", "name", "output", "args", "chars", "status",
        "input_tokens", "output_tokens", "billed_input", "cached_input")


def _go(aurora_rs, tmp_path, core, replies, extra=(), denylist=None):
    model = _Model(list(replies))
    try:
        home, work, cfg, env = _setup(tmp_path / core, model.url)
        cfg.write_text(cfg.read_text() + f"  core: {core}\n")
        env["AURORA_RS"] = aurora_rs
        if denylist:
            (home / "denylist.yaml").write_text(denylist)
        p = _run([sys.executable, "-m", "aurora", "-t", "write the note", *extra], env, work)
    finally:
        model.close()
    _sid, recs, _ = _records(home)
    rows = [{k: r[k] for k in KEYS if k in r} for r in recs if r["event"] not in ("session", "meta")]
    return p, rows, model.requests, work


def _both(aurora_rs, tmp_path, replies, **kw):
    py = _go(aurora_rs, tmp_path, "python", replies, **kw)
    rs = _go(aurora_rs, tmp_path, "rust", replies, **kw)
    return py, rs


def _strip(req):
    return [{k: v for k, v in m.items() if not (k == "content" and m["role"] == "system")} for m in req["messages"]]


def test_a_write_turn_is_the_same_on_both_cores(aurora_rs, tmp_path):
    py, rs = _both(aurora_rs, tmp_path, [_write_call("note.txt", "olá\n"), _sse({"content": "Wrote it."})], extra=("--yes",))
    assert rs[0].returncode == py[0].returncode == 0, rs[0].stderr
    assert "Python core" not in rs[0].stderr, rs[0].stderr
    assert rs[0].stdout == py[0].stdout
    assert rs[1] == py[1]
    assert [_strip(r) for r in rs[2]] == [_strip(r) for r in py[2]]
    assert rs[2][0]["tools"] == py[2][0]["tools"]
    assert (rs[3] / "note.txt").read_text(encoding="utf-8") == "olá\n"


def test_headless_denial_is_the_same_on_both_cores(aurora_rs, tmp_path):
    py, rs = _both(aurora_rs, tmp_path, [_write_call("note.txt", "x\n"), _sse({"content": "ok"})])
    assert rs[0].returncode == py[0].returncode
    assert rs[1] == py[1]
    assert not (rs[3] / "note.txt").exists()


def test_a_command_and_a_read_in_one_turn(aurora_rs, tmp_path):
    replies = [_tool_call("run_command", {"command": "echo hi > a.txt"}, "c1"),
               _tool_call("read_file", {"path": "a.txt"}, "c2"), _sse({"content": "done"})]
    py, rs = _both(aurora_rs, tmp_path, replies, extra=("--yes",))
    assert rs[1] == py[1]
    assert [_strip(r) for r in rs[2]] == [_strip(r) for r in py[2]]


def test_plan_mode_or_missing_binary_falls_back_with_a_notice(aurora_rs, tmp_path):
    model = _Model([_sse({"content": "hi"})])
    try:
        home, work, cfg, env = _setup(tmp_path, model.url)
        cfg.write_text(cfg.read_text() + "  core: rust\n")
        env["AURORA_RS"] = str(tmp_path / "nope")
        p = _run([sys.executable, "-m", "aurora", "-t", "hello"], env, work)
    finally:
        model.close()
    assert p.returncode == 0, p.stderr
    assert "this turn runs on the Python core (aurora-rs not found" in p.stderr + p.stdout
    assert json.dumps(model.requests[0]["messages"][-1]) == json.dumps({"role": "user", "content": "hello"})




class _FE:
    """A front end that answers every approval with `answer` and records what it saw."""

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def on_text(self, chunk):
        self.seen.append(("text", chunk))

    def on_think(self, chunk):
        pass

    def on_tool_start(self, name, args):
        self.seen.append(("start", name))

    def on_tool_result(self, name, output):
        self.seen.append(("result", name, output))

    def on_usage(self, *a):
        pass

    def notify(self, message):
        self.seen.append(("notify", message))

    def invalidate_status(self):
        pass

    def approve(self, tool, args, diff):
        self.seen.append(("approve", tool, args.get("path")))
        return self.answer

    def ask_continue(self, iterations):
        return False

    def ask_secret(self, label):
        return ""

    def secret_challenge(self, context, matches, source_text=""):
        return "stop"

    def cancelled(self):
        return False


import pytest  # noqa: E402


@pytest.mark.parametrize("answer", ["y", ("n", "use a.txt instead"), ("c", "write b.txt"), "s"])
def test_interactive_answers_are_the_same_on_both_cores(aurora_rs, tmp_path, monkeypatch, answer):
    got = {}
    for core in ("python", "rust"):
        model = _Model([_write_call("note.txt", "v\n"), _sse({"content": "fine"})])
        try:
            home, work, cfg, env = _setup(tmp_path / core, model.url)
            cfg.write_text(cfg.read_text() + f"  core: {core}\n")
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            monkeypatch.setenv("AURORA_RS", aurora_rs)
            monkeypatch.chdir(work)
            from aurora.engine import Engine
            e = Engine(str(cfg))
            fe = _FE(answer)
            e.send("write the note", fe)
            if getattr(e, "_rust_core", None):
                e._rust_core.close()
        finally:
            model.close()
        _sid, recs, _ = _records(home)
        rows = [{k: r[k] for k in KEYS if k in r} for r in recs if r["event"] not in ("session", "meta")]
        got[core] = {"records": rows, "front end": [s for s in fe.seen if s[0] != "notify"],
                     "requests": [_strip(r) for r in model.requests], "written": (work / "note.txt").exists()}
    for part in got["python"]:
        assert got["rust"][part] == got["python"][part], (part, got["rust"][part], got["python"][part])
