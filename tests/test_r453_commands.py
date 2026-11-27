"""R453: existing commands behave consistently — card "Aurora - Improve
existing features", part 2 (#9 path-first messages, #10/#11 on/off toggles,
#12 one /copy command)."""

import types

import pytest
import yaml

from aurora import clipboard, paths, ui
from tests.test_core import _mk_engine
from tests.test_web import app  # noqa: F401  (fixture)


# ── #9: a message that starts with a path ──────────────────────────────────
@pytest.mark.parametrize("line", [
    "/etc/hosts has a bad entry, fix it", "/usr/local/bin/x crashes", "/tmp is full"])
def test_path_first_lines_are_messages(line):
    assert paths.starts_with_path(line)


@pytest.mark.parametrize("line", [
    "/compact", "/model", "/copy all", "/no-such-skill", "hello", "/", ""])
def test_commands_are_still_commands(line):
    assert not paths.starts_with_path(line)


def test_the_web_ui_sends_a_path_first_message_and_still_refuses_commands(app):  # noqa: F811
    ok, why = app.s.send("/compact")
    assert not ok and "Compact item" in why             # R458: points at the web equivalent
    ok, why = app.s.send("/cache on")
    assert not ok and "only in the terminal" in why
    ok, why = app.s.send("/etc/hosts has a bad entry")
    assert ok, why


def test_the_tui_sends_an_existing_single_segment_path_to_the_model(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(ui, "_run_turn", lambda eng, fe, text, **k: sent.append(text))
    ui._handle_command(e, ui.TerminalFrontend(), "/tmp is full")
    assert sent == ["/tmp is full"]


def test_an_unknown_command_is_still_reported(tmp_path, monkeypatch, capsys):
    e = _mk_engine(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(ui, "_run_turn", lambda *a, **k: sent.append(a))
    ui._handle_command(e, ui.TerminalFrontend(), "/no-such-skill")
    assert sent == [] and "unknown skill" in capsys.readouterr().out


# ── #10/#11: one on/off rule ────────────────────────────────────────────────
def test_on_off_rule():
    assert ui._on_off("on", False, bare_flips=True) is True
    assert ui._on_off("OFF", True, bare_flips=False) is False
    assert ui._on_off("", True, bare_flips=True) is False      # display toggle flips
    assert ui._on_off("", True, bare_flips=False) is True      # a setting is only shown
    assert ui._on_off("maybe", True, bare_flips=True) is None


def test_multiline_on_never_turns_it_off(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    fe = ui.TerminalFrontend()
    ui._handle_command(e, fe, "/multiline on")
    ui._handle_command(e, fe, "/multiline on")
    assert e.multiline is True
    ui._handle_command(e, fe, "/multiline off")
    assert e.multiline is False
    ui._handle_command(e, fe, "/multiline")                    # bare still flips
    assert e.multiline is True


@pytest.mark.parametrize("cmd,attr,key", [
    ("thinking", "show_thinking", "show_thinking"),
    ("markdown", "render_md", "render_markdown")])
def test_thinking_and_markdown_take_on_off_and_are_saved(tmp_path, monkeypatch, cmd, attr, key):
    e = _mk_engine(tmp_path, monkeypatch)
    fe = ui.TerminalFrontend()
    ui._handle_command(e, fe, f"/{cmd} off")
    assert getattr(fe, attr) is False
    saved = yaml.safe_load(open(e.cfg["_path"]))["runtime"][key]
    assert saved is False
    ui._handle_command(e, fe, f"/{cmd} on")
    assert getattr(fe, attr) is True
    assert yaml.safe_load(open(e.cfg["_path"]))["runtime"][key] is True


@pytest.mark.parametrize("cmd", ["cache", "fallback", "redact", "multiline", "thinking", "markdown"])
def test_a_bad_argument_prints_usage_and_changes_nothing(tmp_path, monkeypatch, capsys, cmd):
    e = _mk_engine(tmp_path, monkeypatch)
    fe = ui.TerminalFrontend()
    before = (e.prompt_cache, e.model_fallback, e.redact_secrets, e.multiline,
              fe.show_thinking, fe.render_md)
    ui._handle_command(e, fe, f"/{cmd} maybe")
    assert "usage" in capsys.readouterr().out
    assert before == (e.prompt_cache, e.model_fallback, e.redact_secrets, e.multiline,
                      fe.show_thinking, fe.render_md)


def test_checking_redact_never_turns_it_off(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    e.set_redact_secrets(True)
    ui._handle_command(e, ui.TerminalFrontend(), "/redact")
    assert e.redact_secrets is True


# ── #12: one /copy command ──────────────────────────────────────────────────
class _CopyEngine:
    def __init__(self):
        self.calls = []

    def last_turn_final_reply(self):
        return "final reply"

    def nth_response(self, n):
        self.calls.append(n)
        return f"response {n}"


@pytest.fixture
def copied(monkeypatch):
    got = []
    monkeypatch.setattr(clipboard, "copy", lambda text: got.append(text) or "test")
    return got


def test_bare_copy_takes_the_last_reply(copied, capsys):
    ui._handle_command(_CopyEngine(), ui.TerminalFrontend(), "/copy")
    assert copied == ["final reply"] and "last reply copied" in capsys.readouterr().out


def test_copy_n_takes_the_nth_last_response(copied):
    e = _CopyEngine()
    ui._handle_command(e, ui.TerminalFrontend(), "/copy 3")
    assert copied == ["response 3"] and e.calls == [3]


def test_copy_bash_takes_the_last_bash_output(copied):
    fe = ui.TerminalFrontend()
    fe._tui = types.SimpleNamespace(_last_bash_output="ls output")
    ui._handle_command(_CopyEngine(), fe, "/copy bash")
    assert copied == ["ls output"]


def test_copy_with_a_bad_argument_reports_it_and_copies_nothing(copied, capsys):
    ui._handle_command(_CopyEngine(), ui.TerminalFrontend(), "/copy abc")
    assert copied == [] and "usage: /copy" in capsys.readouterr().out


def test_the_old_copy_names_still_work(copied):
    ui._handle_command(_CopyEngine(), ui.TerminalFrontend(), "/copy-response")
    assert copied == ["final reply"]
    assert ui._resolve_copy_alias("copy-all", "") == ("copy", "all")
    assert ui._resolve_copy_alias("copy-turn", "") == ("copy", "full")
    assert ui._resolve_copy_alias("copy-last", "") == ("copy", "last")
    assert ui.side_safe_command("/copy-all") and ui.side_safe_command("/copy bash")


def test_copy_last_says_which_one_it_copied(copied, capsys, monkeypatch):
    monkeypatch.setattr(ui, "_last_copyable_text", lambda e, fe: ("out", "command output"))
    ui._handle_command(_CopyEngine(), ui.TerminalFrontend(), "/copy last")
    assert "bash-mode output" in capsys.readouterr().out
