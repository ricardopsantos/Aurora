"""R452: bash mode (`!` / `$`) — card "Aurora - Improve existing features",
part 1 (#1–#8, #29). Commands typed in bash mode run in real bash, can be
cancelled, show output live and their exit code, keep their own history,
carry directory and environment over, colour their output, and refuse
programs that need a terminal."""

import os
import threading
import time

import pytest

from aurora import bashmode, tools, tui, ui
from tests.test_tui import _FakeEngine


@pytest.fixture
def t(monkeypatch, tmp_path):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tui.Tui, "_cols", lambda self: 120)
    return tui.Tui(_FakeEngine())


def _texts(t):
    return [e.get("text", "") if isinstance(e, dict) else e for e in t._chat]


def _run_worker(t, monkeypatch, *lines, timeout=5.0):
    monkeypatch.setattr(t, "_banner", lambda: None)
    monkeypatch.setattr("aurora.bootstrap.load", lambda cwd: ("", None))
    monkeypatch.setattr(t.app, "exit", lambda: None)
    monkeypatch.setattr(ui, "_send_turn", lambda *a, **k: None)
    for line in lines:
        t._inbox.put(line)
    t._inbox.put("/exit")
    th = threading.Thread(target=t._worker, daemon=True)
    th.start()
    th.join(timeout=timeout)
    assert not th.is_alive()


# ── #1: real bash, for bash mode AND the model's run_command ───────────────
needs_bash = pytest.mark.skipif(tools._shell_executable() is None, reason="no bash")


@needs_bash
def test_commands_run_in_bash_not_sh(tmp_path):
    out, code = tools._run_command_once("echo {a,b}; [[ 1 == 1 ]] && echo ok",
                                        str(tmp_path))
    assert code == 0
    assert out.split() == ["a", "b", "ok"]


@needs_bash
def test_the_model_run_command_tool_gets_bash_too(tmp_path):
    out = tools.run_command("arr=(x y); echo ${arr[1]}", cwd=str(tmp_path))
    assert "y" in out and "not found" not in out


@needs_bash
def test_background_jobs_run_in_bash_too(tmp_path):
    job = tools._start_background("echo {1..3}", str(tmp_path))
    for _ in range(100):
        if tools._BG_JOBS[job].done:
            break
        time.sleep(0.02)
    assert "1 2 3" in tools.check_command(job)


# ── #3: live output (runner side) ───────────────────────────────────────────
def test_on_chunk_sees_output_as_it_arrives_and_the_result_is_unchanged(tmp_path):
    seen, at = [], []
    t0 = time.monotonic()

    def on_chunk(piece):
        seen.append(piece)
        at.append(time.monotonic() - t0)

    out, code = tools._run_command_once("echo one; sleep 0.4; echo two",
                                        str(tmp_path), on_chunk=on_chunk)
    assert code == 0 and out == "one\ntwo\n"
    assert "".join(seen) == "one\ntwo\n"
    assert at[0] < 0.3              # "one" arrived before the command finished


def test_on_chunk_decodes_a_character_split_across_reads(tmp_path):
    seen = []
    # é is two bytes; print them with a pause between, as two reads
    tools._run_command_once(r"printf '\xc3'; sleep 0.2; printf '\xa9\n'",
                            str(tmp_path), on_chunk=seen.append)
    assert "".join(seen) == "é\n"


def test_a_failing_on_chunk_never_stops_the_command(tmp_path):
    def boom(_):
        raise RuntimeError("display broke")
    out, code = tools._run_command_once("echo a; echo b", str(tmp_path), on_chunk=boom)
    assert (out, code) == ("a\nb\n", 0)


# ── #6: directory and environment carry over ───────────────────────────────
@needs_bash
def test_chained_cd_export_and_cd_dash_carry_over(tmp_path):
    (tmp_path / "sub").mkdir()
    st = bashmode.initial_state(str(tmp_path))
    _, st = bashmode.run("cd sub && export FOO=bar", st)
    assert st.cwd == str(tmp_path / "sub") and st.env["FOO"] == "bar"
    res, st = bashmode.run("echo $FOO", st)
    assert res.out.strip() == "bar"
    _, st = bashmode.run("cd -", st)
    assert st.cwd == str(tmp_path)


@needs_bash
def test_source_activates_a_venv_like_script(tmp_path):
    (tmp_path / "activate").write_text("export VIRTUAL_ENV=/x\nexport PATH=/x/bin:$PATH\n")
    st = bashmode.initial_state(str(tmp_path))
    _, st = bashmode.run("source activate", st)
    assert st.env["VIRTUAL_ENV"] == "/x"
    assert st.env["PATH"].startswith("/x/bin:")


@needs_bash
def test_the_exit_code_survives_the_state_wrapper(tmp_path):
    st = bashmode.initial_state(str(tmp_path))
    for cmd, want in (("false", 1), ("exit 7", 7), ("true", 0)):
        res, st = bashmode.run(cmd, st)
        assert res.code == want, cmd


@needs_bash
def test_shell_counters_do_not_pile_up(tmp_path):
    st = bashmode.initial_state(str(tmp_path))
    for _ in range(3):
        _, st = bashmode.run("true", st)
    assert "SHLVL" not in st.env
    assert "CLICOLOR_FORCE" not in st.env      # added per run, never carried


@needs_bash
def test_the_side_channel_reads_the_state_but_never_changes_it(tmp_path):
    (tmp_path / "sub").mkdir()
    st = bashmode.initial_state(str(tmp_path))
    res, new = bashmode.run("cd sub && export X=1 && pwd", st, carry=False)
    assert new is st and res.out.strip() == str(tmp_path / "sub")


def test_a_killed_command_keeps_the_old_state(tmp_path):
    st = bashmode.initial_state(str(tmp_path))
    res, new = bashmode.run("cd / && sleep 5", st, timeout=0.3)
    assert res.code is None and new.cwd == str(tmp_path)


def test_plain_cd_is_resolved_without_a_shell_and_bash_gets_the_rest():
    assert bashmode.plain_cd_target("my dir") == "my dir"
    assert bashmode.plain_cd_target("~/x") == "~/x"
    for target in ("-", "$HOME", "*", "~other"):
        assert bashmode.plain_cd_target(target) is None
    assert bashmode.plain_cd_target(None) is None


def test_cd_into_records_oldpwd_so_cd_dash_works(tmp_path):
    (tmp_path / "a").mkdir()
    st = bashmode.initial_state(str(tmp_path))
    st2, err = bashmode.cd_into(st, "a")
    assert err is None and st2.cwd == str(tmp_path / "a")
    assert st2.env["OLDPWD"] == str(tmp_path) and st2.env["PWD"] == st2.cwd
    st3, err = bashmode.cd_into(st2, "missing")
    assert st3 is st2 and "no such file" in err


# ── #4: exit codes ──────────────────────────────────────────────────────────
def test_format_result_shows_a_non_zero_exit_and_nothing_for_success():
    R = bashmode.Result
    assert bashmode.format_result(R("", 0)) == ""
    assert bashmode.format_result(R("", 1)) == "[exit 1]"
    assert bashmode.format_result(R("boom\n", 2)) == "boom\n[exit 2]"
    assert bashmode.format_result(R("part", None), timeout=60) == "part\n[timeout after 60s]"
    assert bashmode.format_result(R("", None, cancelled=True)) == "[cancelled]"


@needs_bash
def test_false_shows_exit_1_instead_of_no_output(t, monkeypatch, tmp_path):
    t._bash_cwd = str(tmp_path)
    _run_worker(t, monkeypatch, "!false", "!true")
    assert any("[exit 1]" in x for x in _texts(t))
    assert t._last_bash_output == ""           # `true` ran last: silent success


# ── #2: cancel ──────────────────────────────────────────────────────────────
def test_a_main_bash_command_is_cancelled_by_the_cancel_event(t, monkeypatch, tmp_path):
    t._bash_cwd = str(tmp_path)
    threading.Timer(0.3, t.fe.cancel_event.set).start()
    t0 = time.monotonic()
    _run_worker(t, monkeypatch, "!sleep 30")
    assert time.monotonic() - t0 < 5
    assert "[cancelled]" in t._last_bash_output


def test_a_side_bash_command_is_cancelled_by_the_side_cancel(t, tmp_path):
    t._bash_cwd = str(tmp_path)
    t._side_inbox.put(("bash", "sleep 30"))
    th = threading.Thread(target=t._side_worker, daemon=True)
    th.start()
    for _ in range(100):
        if t._side_kind == "bash":
            break
        time.sleep(0.01)
    time.sleep(0.2)
    t._side_cancel.set()
    for _ in range(200):
        if not t._side_busy:
            break
        time.sleep(0.02)
    assert not t._side_busy
    assert "[cancelled]" in t._last_bash_output      # #8: side output is recorded too


def test_esc_esc_in_bash_mode_cancels_a_running_command(t):
    t._bash_mode, t._busy = True, True
    t._on_escape(lambda: None)
    t._on_escape(lambda: None)
    assert t._menu_prompt == "Cancel this?"
    t._resolve_menu(0)
    assert t.fe.cancel_event.is_set()
    assert t._bash_mode                              # still in bash mode


def test_esc_esc_in_bash_mode_when_idle_still_offers_to_leave(t):
    t._bash_mode, t._busy = True, False
    t._on_escape(lambda: None)
    t._on_escape(lambda: None)
    assert t._menu_prompt == "Leave bash mode?"


def test_esc_in_bash_mode_clears_a_draft_before_cancelling(t):
    t._bash_mode, t._busy = True, True
    t.input.buffer.text = "ls -la"
    t._on_escape(lambda: None)
    assert t.input.buffer.text == "" and not t.fe.cancel_event.is_set()


# ── #3: live output (TUI side) ──────────────────────────────────────────────
def test_output_is_shown_before_the_command_finishes(t, monkeypatch, tmp_path):
    t._bash_cwd = str(tmp_path)
    seen = {}

    def watch():
        for _ in range(100):
            if any("early" in x for x in _texts(t)):
                seen["while_busy"] = t._busy
                return
            time.sleep(0.02)
    w = threading.Thread(target=watch, daemon=True)
    w.start()
    _run_worker(t, monkeypatch, "!echo early; sleep 0.8; echo late")
    w.join(timeout=3)
    assert seen.get("while_busy") is True
    entries = [e for e in t._chat if isinstance(e, dict) and e.get("kind") == "bash_output"]
    assert len(entries) == 1                         # one growing entry, not two
    assert "early" in entries[0]["text"] and "late" in entries[0]["text"]


# ── #5: separate histories ──────────────────────────────────────────────────
def test_bash_mode_and_prompt_mode_have_their_own_history(t):
    buf = t.input.buffer
    assert buf.history is t._histories["prompt"]
    t._bash_mode = True
    assert buf.history is t._histories["bash"]
    t._bash_mode = False
    assert buf.history is t._histories["prompt"]


def test_the_two_histories_are_separate_files(t):
    t._histories["prompt"].append_string("explain this repo")
    t._histories["bash"].append_string("ls -la")
    assert t._histories["prompt"].get_strings() == ["explain this repo"]
    assert t._histories["bash"].get_strings() == ["ls -la"]
    assert t._histories["prompt"].filename != t._histories["bash"].filename


def test_switching_modes_keeps_a_typed_draft(t):
    t.input.buffer.text = "half typed"
    t._bash_mode = True
    assert t.input.buffer.text == "half typed"


# ── #7: colours and terminal-only programs ─────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "vim notes.txt", "sudo nano /etc/hosts", "top", "htop", "python3", "ssh host",
    "git commit", "FOO=1 emacs x", "tmux"])
def test_programs_that_need_a_terminal_are_detected(cmd):
    assert "needs a terminal" in bashmode.needs_terminal(cmd)


@pytest.mark.parametrize("cmd", [
    "ls -la", "python3 script.py", "ssh host uptime", "git commit -m wip",
    "git commit -am wip", "git commit --no-edit", "git status", "less README.md",
    "echo vim", ""])
def test_ordinary_commands_are_not_flagged(cmd):
    assert bashmode.needs_terminal(cmd) is None


def test_a_terminal_program_is_refused_with_a_hint_and_not_run(t, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(tools, "_run_command_once",
                        lambda *a, **k: calls.append(a) or ("", 0))
    _run_worker(t, monkeypatch, "!vim x")
    assert calls == []
    assert "needs a terminal" in capsys.readouterr().out


def test_colours_are_forced_only_when_output_goes_to_the_screen():
    base = {"PATH": "/bin"}
    assert bashmode.color_env("ls", base)["CLICOLOR_FORCE"] == "1"
    for cmd in ("ls | grep x", "ls > f", "x=$(ls)", "x=`ls`"):
        assert bashmode.color_env(cmd, base) == base, cmd
        assert bashmode._color_prelude(cmd) == ""


def test_copy_last_gets_the_text_without_colour_codes():
    assert bashmode.plain_text("\x1b[01;34mdir\x1b[0m file") == "dir file"


def test_sudo_without_a_password_gets_a_hint():
    assert "sudo -v" in bashmode.failure_hint(
        "sudo apt update", 1, "sudo: a terminal is required to read the password")
    assert bashmode.failure_hint("sudo true", 0, "") is None
    assert bashmode.failure_hint("ls", 2, "a terminal is required") is None


# ── #29: clickable file names keep their colour ─────────────────────────────
def test_a_clickable_file_name_keeps_its_colour_and_is_underlined(t, tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    t._bash_cwd = str(tmp_path)
    frags = t._entry_fragments(
        {"kind": "bash_output", "text": "\x1b[32mnotes.txt\x1b[0m plain.md"}, 0)
    links = [f for f in frags if len(f) > 2]
    styles = {f[1]: f[0] for f in links}
    assert "underline" in styles["notes.txt"] and "32" in styles["notes.txt"] \
        or "green" in styles["notes.txt"]
    assert styles["plain.md"] == "class:link"


# ── #8: bash mode's own timeout ─────────────────────────────────────────────
def test_typed_commands_get_a_longer_timeout_than_the_model_tools():
    assert bashmode.BASH_MODE_TIMEOUT > tools.COMMAND_TIMEOUT
