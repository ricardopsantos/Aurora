"""R311 — `aurora -t` one-shot mode: never prompts, exit code says whether
the task finished."""

import io

import pytest

from aurora import headless
from aurora.providers.base import ToolCall, TurnResult

from tests.test_core import FakeProvider, _mk_engine


def _run(tmp_path, monkeypatch, results, **kw):
    e = _mk_engine(tmp_path, monkeypatch)
    e.redact_secrets = False
    prov = FakeProvider(results)
    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: prov)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    code = headless.run(e, "do it", **kw)
    return code, out.getvalue(), err.getvalue(), e


def _write_call(tmp_path, n="1"):
    return ToolCall(n, "write_file", {"path": str(tmp_path / "w.txt"), "content": "x"})


def test_plain_answer_exits_0_stdout_only_the_answer(tmp_path, monkeypatch):
    code, out, err, _ = _run(tmp_path, monkeypatch,
                             [TurnResult(text="done", stop_reason="end")])
    assert code == 0
    assert out.strip() == "chunk"          # FakeProvider streams "chunk "
    assert "headless" not in out


def test_ungated_tool_runs(tmp_path, monkeypatch):
    f = tmp_path / "r.txt"
    f.write_text("hi")
    code, out, err, _ = _run(tmp_path, monkeypatch, [
        TurnResult(text="", tool_calls=[ToolCall("1", "read_file", {"path": str(f)})],
                   stop_reason="tool_use"),
        TurnResult(text="ok", stop_reason="end")])
    assert code == 0 and "read_file" in err


def test_gated_call_denied_without_yes_exit_1(tmp_path, monkeypatch):
    code, out, err, _ = _run(tmp_path, monkeypatch, [
        TurnResult(text="", tool_calls=[_write_call(tmp_path)], stop_reason="tool_use"),
        TurnResult(text="gave up", stop_reason="end")])
    assert code == 1
    assert not (tmp_path / "w.txt").exists()
    assert "not on the allowlist" in err


def test_yes_lets_a_gated_call_through(tmp_path, monkeypatch):
    code, *_ = _run(tmp_path, monkeypatch, [
        TurnResult(text="", tool_calls=[_write_call(tmp_path)], stop_reason="tool_use"),
        TurnResult(text="ok", stop_reason="end")], yes=True)
    assert code == 0 and (tmp_path / "w.txt").read_text() == "x"


def test_denylist_still_wins_over_yes(tmp_path, monkeypatch):
    from aurora import approve
    monkeypatch.setenv("AURORA_CORE", "python")   # mocks Python's policy; Rust's: test_rust_compat_r482
    monkeypatch.setattr(approve, "is_denied", lambda *a, **k: True)
    code, *_ = _run(tmp_path, monkeypatch, [
        TurnResult(text="", tool_calls=[_write_call(tmp_path)], stop_reason="tool_use"),
        TurnResult(text="ok", stop_reason="end")], yes=True)
    assert code == 1 and not (tmp_path / "w.txt").exists()


def test_step_cap_exits_1(tmp_path, monkeypatch):
    loop = [TurnResult(text="", tool_calls=[ToolCall(str(i), "read_file",
            {"path": "/nonexistent"})], stop_reason="tool_use") for i in range(10)]
    code, out, err, _ = _run(tmp_path, monkeypatch, loop, max_steps=2)
    assert code == 1 and "step cap" in err


def test_allowlisted_gated_call_runs_without_yes(tmp_path, monkeypatch):
    from aurora import approve
    monkeypatch.setenv("AURORA_CORE", "python")   # mocks Python's policy; Rust's: test_rust_compat_r482
    monkeypatch.setattr(approve, "is_allowed", lambda *a, **k: True)
    code, out, err, _ = _run(tmp_path, monkeypatch, [
        TurnResult(text="", tool_calls=[_write_call(tmp_path)], stop_reason="tool_use"),
        TurnResult(text="ok", stop_reason="end")])
    assert code == 0 and (tmp_path / "w.txt").read_text() == "x"
    assert "not on the allowlist" not in err


def test_cli_flag_validation(monkeypatch, capsys, tmp_path):
    from aurora import __main__ as m
    # config.yaml is untracked: a fresh checkout has none, so give AURORA_HOME one
    (tmp_path / "config.yaml").write_text("models: []\n")
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.delenv("AURORA_CONFIG", raising=False)
    for argv in (["--yes"], ["--max-steps", "3"]):
        monkeypatch.setattr("sys.argv", ["aurora", *argv])
        with pytest.raises(SystemExit) as ex:
            m.main()
        assert ex.value.code not in (0, None)
    # with -t, a usage/config error exits 2 so scripts can tell it from a
    # task that ran but did not finish (1)
    for argv in (["-t"], ["-t", "x", "--max-steps", "0"], ["-t", "  "],
                 ["-t", "x", "/nonexistent/config.yaml"],
                 ["-t", "x", "--resume", "nope"]):
        monkeypatch.setattr("sys.argv", ["aurora", *argv])
        with pytest.raises(SystemExit) as ex:
            m.main()
        assert ex.value.code == 2, argv
        assert capsys.readouterr().err.strip()


def test_status_lines_go_to_stderr_in_headless(tmp_path, monkeypatch, capsys):
    from aurora import __main__ as m
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text('providers:\n  local: {type: openai, base_url: "http://x"}\n'
                   'models:\n  - {model: m, provider: local}\n')
    monkeypatch.setattr("sys.argv", ["aurora", "--continue", "-t", "x", str(cfg)])
    monkeypatch.setattr(headless, "run", lambda *a, **k: 0)
    with pytest.raises(SystemExit):
        m.main()
    cap = capsys.readouterr()
    assert cap.out == "" and "starting fresh" in cap.err


def test_dash_reads_task_from_stdin_and_never_opens_a_repl(tmp_path, monkeypatch):
    from aurora import __main__ as m
    seen = {}
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text('providers:\n  local: {type: openai, base_url: "http://x"}\n'
                   'models:\n  - {model: m, provider: local}\n')
    monkeypatch.setattr("sys.argv", ["aurora", "-t", "-", str(cfg)])
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    monkeypatch.setattr(headless, "run",
                        lambda eng, task, **kw: seen.update(task=task, **kw) or 0)
    with pytest.raises(SystemExit) as ex:
        m.main()
    assert ex.value.code == 0 and seen == {"task": "from stdin", "yes": False,
                                           "max_steps": None, "as_json": False}


def test_dead_provider_is_a_failure_not_silent_success(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(e, "send", lambda task, fe: fe.notify("provider error: HTTP 499"))
    err = io.StringIO()
    monkeypatch.setattr("sys.stderr", err)
    assert headless.run(e, "x") == 1
    e2 = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(e2, "send", lambda task, fe: None)   # silent, no text/tools
    assert headless.run(e2, "x") == 1


def test_json_prints_one_object_with_reply_calls_and_cost(tmp_path, monkeypatch):
    import json
    f = tmp_path / "r.txt"
    f.write_text("hi")
    code, out, err, _ = _run(tmp_path, monkeypatch, [
        TurnResult(text="", tool_calls=[ToolCall("1", "read_file", {"path": str(f)})],
                   stop_reason="tool_use"),
        TurnResult(text="ok", stop_reason="end")], as_json=True)
    obj = json.loads(out)                       # stdout is exactly one object
    assert code == 0 and obj["ok"] is True and obj["exit_code"] == 0
    assert obj["reply"].strip() == "chunk chunk" or "chunk" in obj["reply"]
    assert obj["tool_calls"][0]["name"] == "read_file"
    assert "hi" in obj["tool_calls"][0]["result"]
    assert {"reasons", "model", "session", "cost_usd", "context_tokens"} <= set(obj)


def test_json_reports_failure_reasons_and_exit_1(tmp_path, monkeypatch):
    import json
    code, out, err, _ = _run(tmp_path, monkeypatch, [
        TurnResult(text="", tool_calls=[_write_call(tmp_path)], stop_reason="tool_use"),
        TurnResult(text="gave up", stop_reason="end")], as_json=True)
    obj = json.loads(out)
    assert code == 1 and obj["ok"] is False
    assert any("not on the allowlist" in r for r in obj["reasons"])
