"""Tests for the finishing pieces: context bootstrap, skills, engine
compact/resume, LlamaDesk parsing. No network — everything local/mocked."""

import json
import os
import stat
import textwrap

import pytest

from aurora import context, skills
from aurora.engine import Engine
from aurora.session import Session


# ── context bootstrap ──────────────────────────────────────────────────────
def _make_context(root):
    ac = root / ".agentic_context"
    (ac / "KNOWLEDGE" / "core").mkdir(parents=True)
    (ac / "MEMORY").mkdir()
    (ac / "SKILLS").mkdir()
    (ac / "AGENTS.md").write_text("# Rules\n- be terse\n")
    (ac / "KNOWLEDGE" / "INDEX.md").write_text(
        "# KNOWLEDGE\n"
        "- `core/Main.md` — [CORE] always-load doc. (~10 tok)\n"
        "- `core/Lazy.md` — lazy doc. (~10 tok)\n")
    (ac / "MEMORY" / "INDEX.md").write_text("# MEMORY\n- nothing\n")
    (ac / "SKILLS" / "INDEX.md").write_text("# SKILLS\n- nothing\n")
    (ac / "KNOWLEDGE" / "core" / "Main.md").write_text("CORE-BODY-MARKER")
    (ac / "KNOWLEDGE" / "core" / "Lazy.md").write_text("LAZY-BODY-MARKER")
    return ac


def test_bootstrap_loads_rules_indexes_and_core_only(tmp_path):
    _make_context(tmp_path)
    prompt = context.bootstrap(tmp_path)
    assert "be terse" in prompt                 # AGENTS.md
    assert "core/Main.md" in prompt             # index listed
    assert "CORE-BODY-MARKER" in prompt         # [CORE] body loaded
    assert "LAZY-BODY-MARKER" not in prompt     # lazy body NOT loaded
    assert "agentic_context protocol" in prompt
    assert context.active()


def test_open_context_doc_reads_and_confines(tmp_path):
    _make_context(tmp_path)
    context.bootstrap(tmp_path)
    assert context.open_context_doc("KNOWLEDGE/core/Lazy.md") == "LAZY-BODY-MARKER"
    assert "escapes" in context.open_context_doc("../outside.md")
    assert "no such" in context.open_context_doc("KNOWLEDGE/nope.md")


def test_no_context_dir_is_inactive(tmp_path):
    assert context.bootstrap(tmp_path) == ""
    assert not context.active()


# ── skills ─────────────────────────────────────────────────────────────────
def test_skills_discover_and_run(tmp_path):
    sk = tmp_path / "skills"
    sk.mkdir()
    script = sk / "hello.py"
    script.write_text("# says hello\nimport sys\nprint('hi', *sys.argv[1:])\n")
    assert "hello" in skills.discover(str(tmp_path))
    assert "/hello" in skills.listing(str(tmp_path))
    assert skills.run("hello", "world", str(tmp_path)).startswith("hi world")
    assert "unknown skill" in skills.run("nope", "", str(tmp_path))


# ── engine compact / resume ────────────────────────────────────────────────
@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        providers:
          openrouter: {type: openai, api_key_env: FAKE_KEY}
        models:
          - {provider: openrouter, model: some-model, tools: true}
        runtime: {max_iterations: 5}
    """))
    return Engine(str(cfg))


def test_compact_folds_history(engine):
    engine.messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert engine.compact_history() == 2
    assert len(engine.messages) == 1
    body = engine.messages[0]["content"]
    assert "question" in body and "answer" in body
    assert engine.compact_history() == 1  # idempotent-ish: folds again


def test_resume_rebuilds_history(engine):
    past = Session("resumetest01")
    past.log("user", text="first question", model="m")
    past.log("assistant", text="first answer", model="m")
    past.log("tool", name="grep", output="x")  # ignored on resume
    n = engine.resume_from("resumetest01")
    assert n == 2
    assert engine.session.id == "resumetest01"
    assert engine.messages[0]["content"] == "first question"
    assert engine.messages[1]["role"] == "assistant"


# ── llamadesk response-shape tolerance ─────────────────────────────────────
def test_llamadesk_parses_model_shapes(monkeypatch):
    from aurora.llamadesk import LlamaDesk

    desk = LlamaDesk("http://x")
    monkeypatch.setattr(desk, "_get",
                        lambda p: {"models": [{"name": "qwen"}, "gemma"]})
    assert desk.models() == ["qwen", "gemma"]
    monkeypatch.setattr(desk, "_get", lambda p: {"loaded": "qwen"})
    assert desk.loaded_model() == "qwen"


# ── markdown renderer ──────────────────────────────────────────────────────
def test_mdrender_lines(monkeypatch):
    from aurora import mdrender
    monkeypatch.setattr(mdrender, "BOLD", "[B]")
    monkeypatch.setattr(mdrender, "DIM", "[D]")
    monkeypatch.setattr(mdrender, "CYAN", "[C]")
    monkeypatch.setattr(mdrender, "RESET", "[R]")
    r = mdrender.LineRenderer()
    assert r.render("**Folders:**") == "[B]Folders:[R]"
    assert r.render("* item") == "• item"
    assert r.render("## Title") == "[B][C]Title[R]"
    assert r.render("```").startswith("[D]")
    assert r.render("code line") == "[D]code line[R]"  # inside fence
    r2 = mdrender.LineRenderer()
    monkeypatch.setattr(mdrender, "RESET", "")  # colours off → byte-faithful
    assert r2.render("**raw**") == "**raw**"


def test_agent_flushes_parallel_calls_via_bulk_api(tmp_path):
    from aurora import agent, approve
    from aurora.providers.base import ToolCall, TurnResult

    class BulkProvider:
        extra_body = {}; on_think = None
        def __init__(self): self.n = 0; self.bulk_called = 0
        def turn(self, *a, **k):
            self.n += 1
            if self.n == 1:
                return TurnResult(tool_calls=[
                    ToolCall("1", "read_file", {"path": str(tmp_path / "x")}),
                    ToolCall("2", "list_dir", {"path": str(tmp_path)})])
            return TurnResult(text="done")
        def assistant_message(self, r): return {"role": "assistant", "content": r.text or "…"}
        def tool_result_message(self, c, o): raise AssertionError("bulk path not used")
        def tool_results_messages(self, pairs):
            self.bulk_called += 1
            return [{"role": "user", "content": [o for _, o in pairs]}]

    (tmp_path / "x").write_text("hi")
    p = BulkProvider()
    msgs = [{"role": "user", "content": "go"}]
    cb = agent.AgentCallbacks(lambda t: None, lambda n, a: None, lambda n, o: None,
                              lambda t, a, d: "y", lambda i: True,
                              lambda m: None, lambda: False)
    t = agent.run_turn(p, "m", msgs, "", cb, 5, True, False)
    assert p.bulk_called == 1                  # both results in one flush
    assert t.billed_input >= 0


def test_tool_output_truncated(tmp_path):
    from aurora import tools
    big = tmp_path / "big.txt"
    big.write_text("x" * 100_000)
    out = tools.run_tool("read_file", {"path": str(big)})
    assert len(out) < 70_000
    assert "truncated" in out


# ── regression: bugfix pass ────────────────────────────────────────────────
def test_skill_run_survives_exec_format_error(tmp_path):
    # an executable .py without a shebang must return text, not raise OSError
    sk = tmp_path / "skills"
    sk.mkdir()
    bad = sk / "bad.py"
    bad.write_text("print(1)\n")
    bad.chmod(0o755)
    assert "skill error" in skills.run("bad", "", str(tmp_path))


def test_skill_run_passes_quoted_args_as_one(tmp_path):
    sk = tmp_path / "skills"
    sk.mkdir()
    echo = sk / "echo.py"
    echo.write_text("# echo args\nimport sys\nprint(repr(sys.argv[1:]))\n")
    out = skills.run("echo", 'hello "two words"', str(tmp_path))
    assert "'two words'" in out            # one argv entry, not two fragments


def test_records_skips_corrupt_lines(engine):
    s = Session("corruptsession1")
    s.log("user", text="good", model="m")
    with open(s.log_path, "a") as f:       # truncated write / disk-full line
        f.write("{not json\n")
    s.log("assistant", text="also good", model="m")
    rows = s.records()
    assert [r["event"] for r in rows] == ["user", "assistant"]


def test_failed_turn_keeps_previous_context_gauge(engine, monkeypatch):
    # a ProviderError turn must not reset _used to 0 while history is kept
    from aurora.providers.base import ProviderError
    engine.messages = [{"role": "user", "content": "old"},
                       {"role": "assistant", "content": "older"}]
    engine._used = 500

    class _DeadProvider:
        api_key = "k"
        extra_body = {}
        on_think = None

        def turn(self, *a, **k):
            raise ProviderError("backend unreachable")

    monkeypatch.setattr(engine, "_provider_for", lambda *a, **k: _DeadProvider())

    class _FE:
        def on_text(self, c): pass
        def on_tool_start(self, n, a): pass
        def on_tool_result(self, n, o): pass
        def approve(self, t, a, d): return "n", ""
        def ask_continue(self, i): return False
        def notify(self, m): pass
        def cancelled(self): return False
    engine.send("hi", _FE())
    assert engine._used == 500
    assert engine.messages[-1]["role"] == "assistant"   # dangling user popped
