"""R492 — auto-compact in the Rust core folds history as Python's engine does
(R14, R118, R154, R156): same trigger, same summarization request, same
folded history sent next, same `compact` record. Python is the oracle."""

import json
import sys

from tests.test_rust_compat_r482 import ROOT, _Model, _run, _setup, _sse, _write_call, aurora_rs  # noqa: F401


def _seed(home, n=12):
    recs = []
    for i in range(n):
        recs.append({"ts": "2026-11-20T10:00:00+00:00", "event": "user", "text": f"question {i} " + "q" * 900})
        recs.append({"ts": "2026-11-20T10:00:01+00:00", "event": "assistant", "text": f"answer {i} " + "a" * 900})
    d = home / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "c0ffee000001.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def _cfg(cfg, limit, keep):
    text = cfg.read_text(encoding="utf-8")
    text = text.replace("    type: openai\n", f"    type: openai\n    context_limit: {limit}\n", 1)
    text += f"  compact_keep_recent_tokens: {keep}\n  auto_compact_threshold_pct: 80\n"
    cfg.write_text(text, encoding="utf-8")


def _go(argv, tmp_path, replies, summary_fails=False):
    # callers pass folders of equal length ("py", "ru"): the cwd is in the system prompt, and its size in the estimates
    model = _Model(replies)
    try:
        home, work, cfg, env = _setup(tmp_path, model.url)
        env["PYTHONPATH"] = str(ROOT)
        _cfg(cfg, 6000, 1200)
        _seed(home)
        p = _run(argv + ["--continue", "-t", "next task", "--yes"], env, work)
    finally:
        model.close()
    recs = [json.loads(line) for line in (home / "sessions" / "c0ffee000001.jsonl").read_text().splitlines()]
    return p, model.requests, [r for r in recs if r["event"] == "compact"], work


def _strip_sys(msgs):
    return [m for m in msgs if m["role"] != "system"]


def test_fold_before_the_new_prompt(aurora_rs, tmp_path):
    out = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        p, reqs, compacts, _ = _go(argv, tmp_path / name[:2], [_sse({"content": "THE SUMMARY"}), _sse({"content": "done"})])
        assert p.returncode == 0, (name, p.stderr)
        assert "auto-compact: folded" in p.stderr, (name, p.stderr)
        out[name] = (reqs, compacts)
    (py_reqs, py_c), (rs_reqs, rs_c) = out["python"], out["rust"]
    assert len(py_reqs) == len(rs_reqs) == 2
    # the summarization request: same single user message, no tools
    assert _strip_sys(rs_reqs[0]["messages"]) == _strip_sys(py_reqs[0]["messages"])
    assert not rs_reqs[0].get("tools") and not py_reqs[0].get("tools")
    # the turn after the fold carries the same history
    assert _strip_sys(rs_reqs[1]["messages"]) == _strip_sys(py_reqs[1]["messages"])
    assert rs_reqs[1]["messages"][1]["content"].startswith("[Summary of the earlier conversation:]\n\nTHE SUMMARY")
    assert len(py_c) == len(rs_c) == 1
    for k in ("folded", "summarized", "kept_recent", "used_before", "used_after"):
        assert rs_c[0][k] == py_c[0][k], (k, py_c[0], rs_c[0])


def test_failed_summary_falls_back_to_a_clipped_flatten(aurora_rs, tmp_path):
    out = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        p, reqs, compacts, _ = _go(argv, tmp_path / name[:2], [_sse({"content": ""}), _sse({"content": "done"})])
        assert p.returncode == 0, (name, p.stderr)
        out[name] = (reqs, compacts)
    (py_reqs, py_c), (rs_reqs, rs_c) = out["python"], out["rust"]
    assert _strip_sys(rs_reqs[1]["messages"]) == _strip_sys(py_reqs[1]["messages"])
    assert rs_c[0]["summarized"] is False and py_c[0]["summarized"] is False


def test_mid_turn_fold_after_a_big_tool_result(aurora_rs, tmp_path):
    big = "z" * 30000
    out = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        read = _sse({"tool_calls": [{"index": 0, "id": "call_r", "type": "function",
                                     "function": {"name": "read_file", "arguments": json.dumps({"path": "big.txt"})}}]})
        model = _Model([read, _sse({"content": "S2"}), _sse({"content": "done"})])
        try:
            home, work, cfg, env = _setup(tmp_path / name[:2], model.url)
            env["PYTHONPATH"] = str(ROOT)
            _cfg(cfg, 8000, 500)
            (work / "big.txt").write_text(big, encoding="utf-8")
            p = _run(argv + ["-t", "read it", "--yes"], env, work)
        finally:
            model.close()
        assert p.returncode == 0, (name, p.stderr)
        assert "mid-task" in p.stderr, (name, p.stderr)
        out[name] = model.requests
    py, rs = out["python"], out["rust"]
    assert len(py) == 3
    assert len(py) == len(rs), ([len(r["messages"]) for r in py], [len(r["messages"]) for r in rs])
    for a, b in zip(py, rs):
        assert _strip_sys(b["messages"]) == _strip_sys(a["messages"])
