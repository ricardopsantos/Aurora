"""R455: the TUI says only what it checked (card part 4, #23-#33)."""
import re

import pytest

from aurora import agent, tui, ui
from aurora.providers.openai_compat import _is_lan_host
from tests.test_tui import _FakeEngine


@pytest.fixture
def t(monkeypatch):
    monkeypatch.setattr(tui.Tui, "_cols", lambda self: 120)
    return tui.Tui(_FakeEngine())


def _plain(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# ── #23: a bare VPN (100.64.0.0/10) address is a local host, so it is probed ──
@pytest.mark.parametrize("url", ["http://100.64.0.1:11434/v1", "http://100.127.255.254/v1"])
def test_shared_address_range_is_local(url):
    assert _is_lan_host(url)


@pytest.mark.parametrize("url", ["http://100.63.255.255/v1", "http://100.128.0.1/v1",
                                 "https://openrouter.ai/api/v1"])
def test_public_addresses_stay_remote(url):
    assert not _is_lan_host(url)


# ── #25: an Ollama backend gets Ollama advice on the turn-failure path ──────
def test_connection_failure_advice_names_ollama_for_an_ollama_provider(monkeypatch):
    monkeypatch.setattr("aurora.diagnose.server_installed", lambda *a, **k: True)   # R579: Ollama installed here
    from aurora import diagnose
    from aurora.providers.base import ProviderError
    from tests.test_core import _cb

    class _Dead:
        base_url = "http://127.0.0.1:9/v1"
        name = "x"

        def _is_ollama(self):
            return True

        def turn(self, *a, **k):
            raise ProviderError("x request failed: [Errno 111] Connection refused")

    notes = []
    cb = _cb()
    cb.notify = notes.append
    agent.run_turn(_Dead(), "m", [{"role": "user", "content": "hi"}], "", cb, 3, True)
    text = "\n".join(notes)
    assert "ollama serve" in text and "llama.cpp" not in text
    assert diagnose  # imported for the module under test


# ── #26: no "thought for 0s" row when the model never answered ─────────────
def _think_rows(t):
    return [e for e in t._chat if isinstance(e, dict) and e.get("kind") == "think"]


def test_failed_request_leaves_no_think_row(t):
    fe = tui.TuiFrontend(t)
    fe.begin_turn()
    fe.on_request()
    assert _think_rows(t)
    t.append("· local backend unreachable\n")     # what notify prints in the TUI
    fe.end_turn()
    assert _think_rows(t) == []
    assert any("unreachable" in e for e in t._chat if isinstance(e, str))


def test_answered_request_keeps_its_think_row(t):
    fe = tui.TuiFrontend(t)
    fe.begin_turn()
    fe.on_request()
    fe.on_text("hello\n")
    fe.end_turn()
    assert len(_think_rows(t)) == 1


# ── #27: retry lines: one bullet, no blank line between them ───────────────
def test_retry_notices_have_one_bullet_and_no_gap(capsys):
    fe = ui.TerminalFrontend()
    fe.notify("ConnectError — retrying (attempt 2/3)")
    fe.notify("ConnectError — retrying (attempt 3/3)")
    out = _plain(capsys.readouterr().out)
    assert out == "· ConnectError — retrying (attempt 2/3)\n· ConnectError — retrying (attempt 3/3)\n"


def test_provider_retry_message_has_no_bullet_of_its_own():
    import inspect

    from aurora.providers import openai_compat
    src = inspect.getsource(openai_compat)
    assert 'f"· {e.__class__.__name__} — retrying' not in src


# ── #28: the TUI help is short, grouped and TUI-only ───────────────────────
def test_tui_help_is_grouped_and_tui_only():
    text = _plain(ui.TUI_HELP)
    for heading in ("Basics", "Keys", "Commands"):
        assert heading in text.splitlines()
    assert "Classic" not in text and "Ctrl+C" not in text
    assert len(text.splitlines()) <= 30
    for line in text.splitlines():
        if len(line) >= 23 and line[0] in "/!?pECA":
            assert line[21] == " " and line[22] != " ", line


def test_tui_shows_the_tui_help(t):
    shown = "".join(f[1] for f in t._help_text)
    assert "Basics" in shown and "Classic REPL" not in shown


# ── #33: a cancelled model picker says so ──────────────────────────────────
def test_cancelled_model_picker_prints_a_result(tmp_path, monkeypatch, capsys):
    from tests.test_core import _mk_engine
    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(e, "list_models", lambda: [{"provider": "openrouter", "model": "acme/x"}])
    labels = []
    monkeypatch.setattr(ui, "select", lambda prompt, options, **k: (
        labels.extend(_plain(o[1]) for o in options), None)[1])
    ui._pick_model(e, ui.TerminalFrontend())
    assert "cancelled — model unchanged" in _plain(capsys.readouterr().out)


def test_picker_marks_the_current_model_in_words(tmp_path, monkeypatch):
    from tests.test_core import _mk_engine
    e = _mk_engine(tmp_path, monkeypatch)
    cur = dict(e.current)
    monkeypatch.setattr(e, "list_models", lambda: [cur])
    labels = []
    monkeypatch.setattr(ui, "select", lambda prompt, options, **k: (
        labels.extend(_plain(o[1]) for o in options), None)[1])
    ui._pick_model(e, ui.TerminalFrontend())
    assert "(current)" in labels[0] and "✔" not in labels[0]


# ── #31: the completion menu sits above the input box ──────────────────────
def test_completion_menu_floats_above_the_input(t):
    fl = t.app.layout.container.floats[0]
    assert isinstance(fl, tui._AboveInputFloat)
    assert not fl.ycursor
    # input row + its two rules + the 3-row status bar
    assert fl.bottom == 1 + 1 + 1 + 3
