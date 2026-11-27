"""R487 — `aurora-rs --continue / --resume ID` rebuild the same conversation
as Python's `Engine.resume_from` (R20, R128, R170c, R248), from the same
session log, and keep appending to that log. Skipped when cargo is absent."""

import json
import sys

from tests.test_rust_compat_r482 import ROOT, _Model, _run, _setup, _sse, aurora_rs  # noqa: F401

PAST = [
    {"ts": "2026-11-20T10:00:00+00:00", "event": "user", "text": "first"},
    {"ts": "2026-11-20T10:00:01+00:00", "event": "tool", "name": "read_file", "output": "x"},
    {"ts": "2026-11-20T10:00:02+00:00", "event": "user", "text": "retry after an error"},
    {"ts": "2026-11-20T10:00:03+00:00", "event": "assistant", "text": "answer one"},
    {"ts": "2026-11-20T10:00:04+00:00", "event": "assistant", "text": ""},
    {"ts": "2026-11-20T10:00:05+00:00", "event": "compact", "summary": "s"},
    {"ts": "2026-11-20T10:00:06+00:00", "event": "user", "text": "crashed before a reply"},
]


def _seed(home, sid):
    d = home / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in PAST), encoding="utf-8")


def _resume(argv, tmp_path, flag):
    model = _Model([_sse({"content": "ok"})])
    try:
        home, work, _cfg, env = _setup(tmp_path, model.url)
        env["PYTHONPATH"] = str(ROOT)
        _seed(home, "abcdef123456")
        p = _run(argv + flag + ["-t", "next", "--yes"], env, work)
    finally:
        model.close()
    return p, home, model.requests


def test_resume_sends_python_history(aurora_rs, tmp_path):
    out = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        for flag in (["--continue"], ["--resume", "abcdef123456"]):
            p, home, reqs = _resume(argv, tmp_path / name / flag[0][2:], flag)
            assert p.returncode == 0, (name, p.stderr)
            assert "session abcdef123456 (1 turns)" in p.stderr, (name, p.stderr)
            msgs = [m for m in reqs[0]["messages"] if m["role"] != "system"]
            files = list((home / "sessions").glob("*.jsonl"))
            assert [f.stem for f in files] == ["abcdef123456"], (name, files)   # same log, no fork
            tail = [json.loads(line)["event"] for line in files[0].read_text().splitlines()[len(PAST):]]
            out[(name, flag[0])] = (msgs, tail)
    for flag in ("--continue", "--resume"):
        py, rs = out[("python", flag)], out[("rust", flag)]
        assert rs[0] == py[0], flag
        assert py[0] == [{"role": "user", "content": "first\n\nretry after an error"},
                         {"role": "assistant", "content": "answer one"},
                         {"role": "user", "content": "next"}]
        assert "user" in rs[1] and "assistant" in rs[1]


def test_unknown_resume_id_is_a_usage_error(aurora_rs, tmp_path):
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        home, work, _cfg, env = _setup(tmp_path / name, "http://127.0.0.1:9/v1")
        env["PYTHONPATH"] = str(ROOT)
        p = _run(argv + ["--resume", "nope", "-t", "x"], env, work)
        assert p.returncode == 2, (name, p.stderr)
        assert "no session nope on this machine" in p.stderr
