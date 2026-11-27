"""R495 — the Rust core runs Python's extensions (bundled and the user's own)
through the aurora-ext/1 host (R477), so the model sees the same tools, in the
same order, and gets the same results as in Python."""

import json
import sys

from tests.test_rust_compat_r482 import ROOT, _Model, _records, _run, _setup, _sse, _tool_call, aurora_rs  # noqa: F401

EXT = '''
SPEC = [{"name": "shout", "description": "Upper-case a word.",
         "parameters": {"type": "object", "properties": {"w": {"type": "string"}}}},
        {"name": "boom", "description": "Always fails.", "parameters": {"type": "object", "properties": {}}}]
def shout(w="", **_):
    print("noise on stdout")
    return w.upper() + "!"
def boom(**_):
    raise ValueError("nope")
RUNNERS = {"shout": shout, "boom": boom}
'''
BROKEN = "raise RuntimeError('broken on import')\n"


def _go(argv, tmp_path, replies, web=True):
    model = _Model(replies)
    try:
        home, work, cfg, env = _setup(tmp_path, model.url)
        if not web:
            cfg.write_text(cfg.read_text() + "  web_search: false\n")
        env.update(PYTHONPATH=str(ROOT), AURORA_PYTHON=sys.executable)
        (home / "extensions").mkdir()
        (home / "extensions" / "shouter.py").write_text(EXT)
        (home / "extensions" / "zz_broken.py").write_text(BROKEN)
        p = _run(argv + ["-t", "go", "--yes"], env, work)
    finally:
        model.close()
    assert p.returncode == 0, p.stderr
    return home, p, model.requests


def _tools(req):
    return [t["function"] for t in req.get("tools", [])]


def _outputs(home):
    _sid, recs, _ = _records(home)
    return [(r.get("name"), r.get("output")) for r in recs if r.get("event") == "tool"]


def test_same_tools_and_results_as_python(aurora_rs, tmp_path):
    got = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        replies = [_tool_call("shout", {"w": "hey"}, "c1"), _tool_call("boom", {}, "c2"), _sse({"content": "done"})]
        home, p, reqs = _go(argv, tmp_path / name, replies)
        warn = [ln for ln in p.stderr.splitlines() if "zz_broken" in ln]
        got[name] = (_tools(reqs[0]), _outputs(home), warn)
    assert got["rust"] == got["python"]
    names = [t["name"] for t in got["rust"][0]]
    assert "web_fetch" in names and "shout" in names
    assert got["rust"][1][0][1] == "HEY!"
    assert "nope" in got["rust"][1][1][1]
    assert got["rust"][2] and "failed to load" in got["rust"][2][0]


def test_web_search_off_hides_the_web_tools(aurora_rs, tmp_path):
    got = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        _home, _p, reqs = _go(argv, tmp_path / name, [_sse({"content": "ok"})], web=False)
        got[name] = [t["name"] for t in _tools(reqs[0])]
    assert got["rust"] == got["python"]
    assert "web_fetch" not in got["rust"]

