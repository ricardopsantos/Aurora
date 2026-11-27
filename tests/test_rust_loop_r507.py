"""R507 — loop rules the bare-call tests found missing on the Rust core, checked on
both loops with the same provider object and callbacks: the spend limit (R340), the
auto-continue backstop (R314), "allow for this session" across cores (R334), and the
allowlist re-read at each turn (R196)."""

import pytest

from aurora import agent, approve, rustcore
from aurora.providers.base import ToolCall, TurnResult
from tests.conftest import _LoopOnlyEngine
from tests.test_core import FakeProvider, _cb
from tests.test_rust_compat_r482 import aurora_rs  # noqa: F401

REAL = agent.run_turn


def _turn(core, tmp_path, prov, cb, max_iterations=5):
    msgs = [{"role": "user", "content": "go"}]
    if core == "python":
        t = REAL(prov, "m", msgs, "sys", cb, max_iterations, True)
        return t, msgs
    cfg = tmp_path / f"cfg{len(list(tmp_path.glob('cfg*')))}.yaml"
    cfg.write_text("providers:\n  p:\n    type: openai\n    base_url: http://127.0.0.1:9/v1\n"
                   "models:\n  - provider: p\n    model: m\n    tools: true\nruntime:\n  sandbox: off\n")
    e = _LoopOnlyEngine(cfg, prov, msgs, "sys", max_iterations)
    try:
        return rustcore.run_turn(e, prov, "m", cb, True), msgs
    finally:
        if getattr(e, "_rust_core", None):
            e._rust_core.close()


@pytest.fixture
def rs(aurora_rs, monkeypatch, tmp_path):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))   # approve.save must never reach the real allowlist
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AURORA_RS", aurora_rs)
    monkeypatch.delenv("AURORA_CORE", raising=False)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_the_spend_limit_stops_a_turn_before_its_tools_run(core, tmp_path, rs):
    w = tmp_path / "w.txt"
    log = []
    cb = _cb(log=log)
    cb.budget = lambda: ("stop", "stopped: daily spend limit $1.00 reached")
    prov = FakeProvider([TurnResult(tool_calls=[ToolCall("1", "write_file", {"path": str(w), "content": "x"})]),
                         TurnResult(text="no")])
    _t, msgs = _turn(core, tmp_path, prov, cb)
    assert not w.exists()
    assert ("notify", "stopped: daily spend limit $1.00 reached") in log
    assert "spend limit reached" in str(msgs)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_auto_continue_asks_again_at_the_backstop(core, tmp_path, rs, monkeypatch):
    monkeypatch.setattr(agent, "AUTO_CONTINUE_MAX_ROUNDS", 3)
    asked = []
    tc = lambda i: TurnResult(tool_calls=[ToolCall(str(i), "read_file", {"path": f"/nonexistent{i}"})])  # noqa: E731
    prov = FakeProvider([tc(1), tc(2), tc(3), tc(4), TurnResult(text="done", stop_reason="end")])
    cb = _cb()
    cb.ask_continue = lambda i: asked.append(i) or True
    cb.auto_continue = lambda: True
    _turn(core, tmp_path, prov, cb, max_iterations=1)
    assert asked and asked[0] == 3


def test_allow_for_this_session_outlives_the_core(tmp_path, rs):
    approve.clear_session_rules()
    asked = []
    cb = _cb()
    cb.approve = lambda t, a, d: asked.append(a["command"]) or "t"
    for _ in range(2):                  # two turns, each on a new core process
        prov = FakeProvider([TurnResult(tool_calls=[ToolCall("1", "run_command", {"command": "echo hi there"})]),
                             TurnResult(text="ok")])
        _turn("rust", tmp_path, prov, cb)
    assert asked == ["echo hi there"]
    assert approve.session_rules().get("run_command")
    approve.clear_session_rules()


def test_an_allowlist_rule_added_between_turns_is_honoured(tmp_path, rs):
    w = tmp_path / "w.txt"
    asked = []
    cb = _cb()
    cb.approve = lambda t, a, d: asked.append(t) or "n"
    approve.save({"write_file": []})
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("providers:\n  p:\n    type: openai\n    base_url: http://127.0.0.1:9/v1\n"
                   "models:\n  - provider: p\n    model: m\n    tools: true\nruntime:\n  sandbox: off\n")
    prov = FakeProvider([TurnResult(tool_calls=[ToolCall("1", "write_file", {"path": str(w), "content": "x"})]),
                         TurnResult(text="no"),
                         TurnResult(tool_calls=[ToolCall("2", "write_file", {"path": str(w), "content": "x"})]),
                         TurnResult(text="yes")])
    msgs = [{"role": "user", "content": "go"}]
    e = _LoopOnlyEngine(cfg, prov, msgs, "sys", 5)
    try:
        rustcore.run_turn(e, prov, "m", cb, True)            # asked, denied
        approve.save({"write_file": ["*"]})                   # the user allows it (another window, /permissions)
        msgs.append({"role": "user", "content": "again"})
        rustcore.run_turn(e, prov, "m", cb, True)            # the same core: not asked, written
    finally:
        e._rust_core.close()
    assert asked == ["write_file"] and w.read_text() == "x"
