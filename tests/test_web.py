import base64
import re
"""R353 — Aurora Lite (`aurora --web`): token gate, no commands, one turn at a time,
approvals limited to yes / no / stop, the `local` model, engine untouched by the web package."""
import ast
import json
import pathlib
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from aurora.providers.base import ToolCall, TurnResult
from aurora.web import core as web_core
from aurora.web import server as web_server
from aurora.web.core import WebSession
from tests.test_core import FakeProvider

CFG = """
providers:
  local: {type: openai, base_url: "http://x"}
models:
  - {model: other, provider: local}
  - {model: local, provider: local, tools: true}
"""
TOKEN = "t0k"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CFG)
    from aurora.engine import Engine
    e = Engine(str(cfg))
    e.redact_secrets = False
    e.current = next(m for m in e.models if m["model"] == "local")
    monkeypatch.setattr(e, "_warn_if_asleep", lambda fe: None)   # would probe a fake provider
    state = {"prov": FakeProvider([TurnResult(text="hi", stop_reason="end")])}
    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: state["prov"])
    sess = WebSession(e, approve_timeout=0.4, auto_approve=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_server.make_handler(sess, TOKEN))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield type("A", (), {"e": e, "s": sess, "base": base, "state": state, "tmp": tmp_path})
    httpd.shutdown()


def call(app, path, body=None, token=TOKEN):
    req = urllib.request.Request(app.base + path, data=None if body is None else json.dumps(body).encode(),
                                 method="POST" if body is not None else "GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as ex:
        return ex.code, json.loads(ex.read() or b"{}")


def wait_for(pred, secs=5):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def types(app):
    return [ev["type"] for ev in app.s.events]


def _page() -> str:
    """The v1 page together with the script it shares with /v2 (the needles below live in either)."""
    d = pathlib.Path(web_server.__file__).parent
    return (d / "index.html").read_text() + "\n" + (d / "app.js").read_text()


def test_every_api_call_needs_the_token(app):
    assert call(app, "/api/state", token=None)[0] == 401
    assert call(app, "/api/state", token="wrong")[0] == 401
    assert call(app, "/api/send", {"text": "x"}, token=None)[0] == 401
    assert call(app, "/api/state")[0] == 200


def test_the_page_itself_is_served_without_a_token(app):
    with urllib.request.urlopen(app.base + "/", timeout=5) as r:
        assert r.status == 200 and b"Aurora" in r.read()


def test_server_refuses_to_start_without_a_token(app, monkeypatch):
    monkeypatch.delenv("AURORA_WEB_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        web_server.serve(app.e, "127.0.0.1", 0, str(app.tmp))


def test_serve_pins_the_local_model_without_touching_remembered_state(app, monkeypatch):
    monkeypatch.setenv("AURORA_WEB_TOKEN", TOKEN)
    app.e.current = next(m for m in app.e.models if m["model"] == "other")
    monkeypatch.setattr(web_server, "Server",
                        lambda *a, **k: type("H", (), {"serve_forever": lambda s: None, "daemon_threads": 0})())
    cwd = pathlib.Path.cwd()
    try:
        web_server.serve(app.e, "127.0.0.1", 0, str(app.tmp))
    finally:
        import os
        os.chdir(cwd)
    assert app.e.current["model"] == "local"
    from aurora import config
    assert config.load_state().get("last_model") != "local"


def test_slash_commands_are_refused(app):
    code, d = call(app, "/api/send", {"text": "/model foo"})
    assert code == 400 and "disabled" in d["error"]
    assert "user" not in types(app)


def test_a_turn_streams_user_text_and_done(app):
    assert call(app, "/api/send", {"text": "hello"})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    assert types(app)[:2] == ["session", "user"] and "text" in types(app)


def test_new_chat_is_refused_while_busy(app):
    gate = threading.Event()
    prov = app.state["prov"]
    orig = prov.turn
    prov.turn = lambda *a, **k: (gate.wait(3), orig(*a, **k))[1]
    assert call(app, "/api/send", {"text": "one"})[0] == 200
    assert call(app, "/api/new", {})[0] == 409
    gate.set()
    assert wait_for(lambda: "done" in types(app))
    assert call(app, "/api/new", {})[0] == 200 and "reset" in types(app)


def _write_turn(app, name="w.txt"):
    f = app.tmp / name
    app.state["prov"] = FakeProvider([
        TurnResult(text="", stop_reason="tool_calls", tool_calls=[ToolCall("1", "write_file", {"path": str(f), "content": "x"})]),
        TurnResult(text="done", stop_reason="end")])
    return f


def _pending(app):
    assert wait_for(lambda: app.s.state()["pending_approval"])
    return app.s.state()["pending_approval"]


def test_approve_yes_runs_the_write(app):
    f = _write_turn(app)
    call(app, "/api/send", {"text": "go"})
    aid = _pending(app)
    assert call(app, "/api/approve", {"id": aid, "answer": "y"})[0] == 200
    assert wait_for(lambda: "done" in types(app)) and f.read_text() == "x"


def test_approve_no_blocks_the_write(app):
    f = _write_turn(app)
    call(app, "/api/send", {"text": "go"})
    call(app, "/api/approve", {"id": _pending(app), "answer": "n"})
    assert wait_for(lambda: "done" in types(app)) and not f.exists()


def test_no_answer_in_time_is_a_denial(app):
    f = _write_turn(app)
    call(app, "/api/send", {"text": "go"})
    assert wait_for(lambda: "done" in types(app)) and not f.exists()


def test_always_allow_is_not_offered(app):
    _write_turn(app)
    call(app, "/api/send", {"text": "go"})
    aid = _pending(app)
    for ans in ("a", "d", "c", "e", "yes"):
        assert call(app, "/api/approve", {"id": aid, "answer": ans})[0] == 409
    assert call(app, "/api/approve", {"id": aid + 5, "answer": "y"})[0] == 409
    call(app, "/api/approve", {"id": aid, "answer": "n"})
    wait_for(lambda: "done" in types(app))


def test_stop_ends_a_waiting_approval(app):
    f = _write_turn(app)
    app.s.approve_timeout = 30
    call(app, "/api/send", {"text": "go"})
    _pending(app)
    assert call(app, "/api/stop", {})[0] == 200
    assert wait_for(lambda: "done" in types(app)) and not f.exists()


def test_bad_approve_bodies_are_400(app):
    assert call(app, "/api/approve", {"id": True, "answer": "y"})[0] == 400
    assert call(app, "/api/approve", {"id": "1", "answer": "y"})[0] == 400


def test_engine_modules_never_import_the_web_package():
    pkg = pathlib.Path(__file__).resolve().parent.parent / "aurora"
    for p in pkg.rglob("*.py"):
        if "web" in p.parts or p.name == "__main__.py":
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            mod = (node.module or "") if isinstance(node, ast.ImportFrom) else (
                node.names[0].name if isinstance(node, ast.Import) else "")
            assert not mod.endswith("web") and ".web." not in mod, f"{p.name} imports {mod}"


def test_r354_state_says_whether_a_model_is_loaded_and_events_carry_a_time(app):
    code, st = call(app, "/api/state")
    assert code == 200 and st["loaded"] is bool(st["context"].get("live_model"))
    app.e._live_model_name_nonblocking = lambda provider: "models/qwen-test.gguf"
    assert call(app, "/api/state")[1]["loaded"] is True
    call(app, "/api/send", {"text": "hello"})
    assert wait_for(lambda: "done" in types(app))
    assert all(isinstance(ev["t"], float) for ev in app.s.events)


def test_r355_page_renders_markdown_safely_and_remembers_the_choice():
    page = _page()
    assert "function mdRender" in page and "aurora_web_md" in page and 'id="md-on"' in page
    assert "innerHTML" not in page and "insertAdjacentHTML" not in page   # model text never becomes HTML
    assert "https?:" in page                                              # links limited to http(s)


# ── R356: session id, context meter, command sidebar ─────────────────────

def test_session_id_is_in_state_and_announced_first(app):
    code, d = call(app, "/api/state")
    assert code == 200 and d["session_id"] == app.e.session.id
    first = app.s.events[0]
    assert first["type"] == "session" and first["session"] == app.e.session.id and first["id"] == 1


def test_clear_announces_the_session_again(app):
    assert call(app, "/api/command", {"name": "clear"}) == (200, {"ok": True})
    assert types(app)[-2:] == ["reset", "session"]


def test_commands_are_an_allowlist(app):
    for bad in ("model", "/clear", "quit", "", "CLEAR", "clear; rm"):
        code, d = call(app, "/api/command", {"name": bad})
        assert code == 400 and d["error"] == "unknown command", bad
    assert call(app, "/api/command", {"name": "clear"}, token=None)[0] == 401


def test_context_command_reports_used_over_limit(app, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(app.e, "context_stats", lambda: SimpleNamespace(used=500, limit=1000, local_model=None))
    assert call(app, "/api/command", {"name": "context"})[0] == 200
    assert "500 / 1000 tokens (50%)" in app.s.events[-1]["text"]


def test_compact_with_nothing_to_fold_says_so_and_frees_the_turn_slot(app):
    assert call(app, "/api/command", {"name": "compact"})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    assert any("nothing to compact" in e.get("text", "") for e in app.s.events)
    assert app.s.busy is False


def test_commands_are_refused_while_a_turn_runs(app):
    app.s.busy = True
    assert call(app, "/api/command", {"name": "clear"})[0] == 409
    assert call(app, "/api/command", {"name": "compact"})[0] == 409
    app.s.busy = False


def test_a_turn_ending_at_90_percent_clears_the_history_with_a_note(app, monkeypatch):
    import dataclasses
    real = app.e.context_stats
    monkeypatch.setattr(app.e, "context_stats", lambda: dataclasses.replace(real(), used=95, limit=100))
    assert call(app, "/api/send", {"text": "hello"})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    assert app.e.messages == [] and "error" not in " ".join(e.get("text", "") for e in app.s.events)
    note = [e for e in app.s.events if e["type"] == "notify" and "history cleared" in e.get("text", "")]
    assert note and "95% full" in note[0]["text"]
    assert "reset" not in types(app)          # the page keeps showing the chat


def test_a_turn_well_below_90_percent_keeps_the_history(app, monkeypatch):
    import dataclasses
    real = app.e.context_stats
    monkeypatch.setattr(app.e, "context_stats", lambda: dataclasses.replace(real(), used=50, limit=100))
    assert call(app, "/api/send", {"text": "hello"})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    assert app.e.messages and not any("history cleared" in e.get("text", "") for e in app.s.events)


def test_sessions_list_marks_the_current_one_with_a_short_id(app):
    assert call(app, "/api/send", {"text": "hello there"})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    code, d = call(app, "/api/sessions")
    assert code == 200
    cur = [x for x in d["sessions"] if x["current"]]
    assert cur and cur[0]["id"] == app.e.session.id and cur[0]["short"] == app.e.session.id[:6]
    assert "hello there" in cur[0]["first"]
    assert call(app, "/api/sessions", token=None)[0] == 401


def test_page_has_the_sidebar_meter_and_clear_on_open():
    page = _page()
    for needle in ('id="side"', 'data-cmd="clear"', 'data-cmd="compact"', 'data-cmd="context"', 'id="meter"',
                   "aurora_web_sidebar", "aurora_web_clear_open", "pct >= 90 ? 'bad' : pct >= 70 ? 'warn'",
                   "case 'session'", "/api/command"):
        assert needle in page, needle
    assert "id=\"new\"" not in page and "confirm('" not in page


def test_session_events_keep_their_own_event_id(app):
    call(app, "/api/command", {"name": "clear"})
    ids = [e["id"] for e in app.s.events]
    assert all(isinstance(i, int) for i in ids) and ids == sorted(ids)


def test_r357_auto_approve_is_on_by_default_and_switchable(app):
    assert WebSession(app.e).auto is True          # the default for a real `aurora --web` (R392: kept on the web session)
    app.s.auto = False
    assert call(app, "/api/state")[1]["auto_approve"] is False
    assert call(app, "/api/auto", {"on": "yes"})[0] == 400
    assert call(app, "/api/auto", {"on": True})[0] == 200 and app.s.auto is True
    assert call(app, "/api/state")[1]["auto_approve"] is True
    assert call(app, "/api/auto", {"on": True}, token="")[0] == 401


def test_r358_state_names_the_workspace_context_file(app, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert call(app, "/api/state")[1]["context_file"] == ""            # no file: the button stays disabled
    (tmp_path / "context.md").write_text("# boot")
    assert call(app, "/api/state")[1]["context_file"] == str(tmp_path / "context.md")
    page = _page()
    assert 'id="load-ctx"' in page and "'load ' + ctxFile" in page      # fills the input, never sends


def test_r359_a_past_session_can_be_read_but_only_by_exact_id(app):
    call(app, "/api/send", {"text": "hello there"})
    assert wait_for(lambda: "done" in types(app))
    sid = call(app, "/api/state")[1]["session_id"]
    code, d = call(app, f"/api/sessions/{sid}")
    assert code == 200 and "hello there" in d["markdown"] and d["id"] == sid
    assert call(app, "/api/sessions/..%2F..%2Fetc%2Fpasswd")[0] == 404
    assert call(app, "/api/sessions/" + sid[:6])[0] == 404          # a prefix is not an id
    assert call(app, f"/api/sessions/{sid}", token="")[0] == 401


def test_r360_page_layout_and_session_list_fixes():
    page = _page()
    head = page.split("</header>")[0]
    assert 'id="token"' not in head and 'id="md-on"' not in head and 'id="auto-on"' in head   # token + markdown live in the sidebar
    assert 'id="token"' in page.split("</header>")[1].split('<main>')[0]
    assert "box.replaceChildren(...rows)" in page and "seq !== sessionsSeq" in page            # no double list from overlapping loads
    assert "tools.findIndex(t => t.name === ev.name)" in page and "toolEl" not in page          # parallel results pair with their call
    assert ".btn.primary:disabled" in page and "position:sticky;top:var(--hh" in page
    assert "min-height:44px" in page and "gap:4px" not in page


def test_r361_token_field_is_not_a_password_field():
    page = _page()
    assert 'type="password"' not in page                               # a password field makes browsers pop up their credential suggestions
    assert 'id="token" class="secret"' in page and "-webkit-text-security:disc" in page and 'autocomplete="off"' in page


def test_r362_closed_dialog_is_hidden_and_tool_calls_can_be_hidden():
    page = _page()
    assert "#view:not([open]){display:none}" in page                    # a display:flex rule on a closed <dialog> showed an empty frame under the header
    assert 'id="hide-tools"' in page and "body.hide-tools details.tool{display:none}" in page and "aurora_web_hide_tools" in page
    assert "getItem('aurora_web_hide_tools') !== '0'" in page             # on unless the user switched it off
    assert page.count("'tool')") == 2                                    # both the call and an unpaired result row carry the class


def test_r363_favicon_is_the_dashed_face_logo():
    """R363 was replaced by R398/R399: the tab and home-screen icons are the sidebar logo (PNG), no longer the SVG face."""
    assert '<link rel="icon" type="image/png" href="/favicon.png">' in _page()


def test_r364_the_page_always_says_what_aurora_is_doing():
    page = _page()
    assert 'id="live"' in page and "function live(txt)" in page
    for needle in ("live('thinking…')", "live('writing…')", "live('running ' + ev.name", "live('working…')"):
        assert needle in page, needle
    assert "function paintLive()" in page and "setInterval(paintLive, 1000)" in page   # a counter runs in every state
    assert "live('');" in page                                          # cleared when the turn ends


def test_r365_tapping_the_status_expands_thinking_and_commands():
    page = _page()
    for needle in ('id="live-line"', 'id="live-think"', "function toggleLive()", "$('status').onclick = statusTap", "else toggleLive()",   # R426: idle wakes, busy expands
                   "'THINKING\\n'", "'COMMANDS\\n'", "turnTools.push("):
        assert needle in page, needle


def test_r366_token_row_fits_the_sidebar():
    page = _page()
    assert ".token-row input{flex:1 1 auto;min-width:0;width:100%}" in page and "aside .btn{width:100%}" not in page   # that rule had squeezed the token field to ~18 px


def test_r367_sidebar_sections_fold_and_are_remembered():
    page = _page()
    for k in ("context", "commands", "options", "sessions", "token"):
        assert f'data-fold="{k}"' in page, k
    for needle in ("aurora_web_folds", "aside section.folded .fold-body{display:none}", 'class="opt"', "data-folded-by-default",
                   "fold(document.querySelector('[data-fold=token]'), true)"):
        assert needle in page, needle
    assert page.count('class="opt"') == 3 and 'label class="tag"><input type="checkbox" id="md-on"' not in page


def _get(app, path):
    with urllib.request.urlopen(app.base + path, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode()


def test_r369_v2_page_is_served_beside_the_original_without_a_token(app):
    st, ct, body = _get(app, "/paper")
    assert st == 200 and ct.startswith("text/html") and "flavour 2 (Paper)" in body
    st, ct, js = _get(app, "/app.js")
    assert st == 200 and "javascript" in ct and "function handle(ev)" in js
    st, _, v1 = _get(app, "/v1")
    assert st == 200 and "flavour 1 (Desktop)" in v1


def test_r369_both_pages_have_every_element_the_shared_script_uses():
    d = pathlib.Path(web_server.__file__).parent
    js = (d / "app.js").read_text().split("// ── R377: the Chat page")[0]   # the Chat-only block uses ids only /v2 has (test_r377_*)
    ids = set(re.findall(r"\$\('([a-z-]+)'\)", js))
    for name in ("index.html", "index_paper.html", "index_v2.html"):
        html = (d / name).read_text()
        missing = sorted(i for i in ids if f'id="{i}"' not in html)
        assert not missing, (name, missing)
    v2 = (d / "index_paper.html").read_text()
    assert "<header>" in v2 and "max-width:720px" in v2 and 'type="password"' not in v2
    assert "AURORA_SB_OPEN = false" in v2 and "aurora_web_sidebar_v2" in v2       # the drawer always starts closed


def test_r370_a_reply_is_painted_before_the_turn_ends():
    js = (pathlib.Path(web_server.__file__).parent / "app.js").read_text()
    assert "function flushCur()" in js and js.count("flushCur()") >= 5


def test_r371_v2_enter_sends_and_the_context_bar_sits_under_the_header():
    d = pathlib.Path(web_server.__file__).parent
    js, v2 = (d / "app.js").read_text(), (d / "index_paper.html").read_text()
    assert "AURORA_ENTER_SENDS = true" in v2 and "window.AURORA_ENTER_SENDS" in js
    for needle in ("e.shiftKey", "e.altKey", "setRangeText('\\n'", "e.isComposing", "pointer:coarse"):
        assert needle in js, needle
    assert "AURORA_ENTER_SENDS" not in (d / "index.html").read_text()      # v1 keeps Ctrl/Cmd+Enter
    head = v2.split("</header>")[0]
    assert 'id="meter"' in head and 'id="hint"' in head and 'id="meter"' not in v2.split("</header>")[1]


def test_r372_v2_log_box_scrolls_theme_choice_and_versions():
    d = pathlib.Path(web_server.__file__).parent
    js, v2 = (d / "app.js").read_text(), (d / "index_paper.html").read_text()
    assert "AURORA_LOG_SCROLLS = true" in v2 and "window.AURORA_LOG_SCROLLS" in js and "if (stick) log.scrollTop = log.scrollHeight" in js
    assert "#log{" in v2 and "overflow-y:auto" in v2.split("#log{")[1].split("}")[0] and "form#send{flex:none" in v2 and "height:100dvh" in v2
    for needle in ('data-theme-set="auto"', 'data-theme-set="light"', 'data-theme-set="dark"', "aurora_web_theme", "delete document.documentElement.dataset.theme"):
        assert needle in v2, needle
    assert 'id="ver-code"' in v2 and 'id="ver-ui"' in v2 and "AURORA_UI_VERSION" in v2


def test_r372_state_carries_the_code_version_shared_with_the_terminal(app):
    import aurora
    st, body = call(app, "/api/state")
    assert st == 200 and body["version"] == str(aurora.__version__)


def _gated(app, n):
    """n scripted turns; the first one waits on the returned gate. `seen` records the order the turns started."""
    app.state["prov"] = FakeProvider([TurnResult(text=f"r{i}", stop_reason="end") for i in range(n)])
    gate, seen = threading.Event(), []
    prov = app.state["prov"]
    orig = prov.turn

    def turn(model, messages, *a, **k):
        seen.append(messages[-1]["content"])
        if len(seen) == 1:
            gate.wait(3)
        return orig(model, messages, *a, **k)
    prov.turn = turn
    return gate, seen


def test_r373_messages_sent_while_busy_are_queued_and_run_one_at_a_time_in_order(app):
    gate, seen = _gated(app, 3)
    assert call(app, "/api/send", {"text": "one"})[0] == 200
    assert wait_for(lambda: seen == ["one"])
    assert call(app, "/api/send", {"text": "two"})[0] == 200 and call(app, "/api/send", {"text": "three"})[0] == 200
    q = [e for e in app.s.events if e["type"] == "queued"]
    assert [(e["text"], e["position"]) for e in q] == [("two", 1), ("three", 2)]
    assert call(app, "/api/state")[1]["queued"] == 2 and seen == ["one"]      # nothing overlaps
    gate.set()
    assert wait_for(lambda: types(app).count("done") == 3)
    assert seen == ["one", "two", "three"]
    users = [e["text"] for e in app.s.events if e["type"] == "user"]
    assert users == ["one", "two", "three"]
    ts = types(app)
    assert ts.index("done") < ts.index("user", ts.index("user") + 1)           # a turn is announced finished before the next starts
    assert call(app, "/api/state")[1]["queued"] == 0 and not app.s.busy


def test_r373_stop_drops_the_queue(app):
    gate, seen = _gated(app, 3)
    call(app, "/api/send", {"text": "one"})
    assert wait_for(lambda: seen == ["one"])
    call(app, "/api/send", {"text": "two"})
    call(app, "/api/stop", {})
    cleared = [e for e in app.s.events if e["type"] == "queue_cleared"]
    assert cleared and cleared[0]["count"] == 1
    gate.set()
    assert wait_for(lambda: not app.s.busy)
    assert seen == ["one"]                                                      # "two" never ran


def test_r373_queue_has_a_limit(app):
    gate, seen = _gated(app, 1)
    call(app, "/api/send", {"text": "one"})
    assert wait_for(lambda: seen == ["one"])
    for i in range(web_core.MAX_QUEUE):
        assert call(app, "/api/send", {"text": f"m{i}"})[0] == 200
    code, d = call(app, "/api/send", {"text": "one too many"})
    assert code == 409 and "full" in d["error"]
    app.s.stop()
    gate.set()
    assert wait_for(lambda: not app.s.busy)


def test_r373_page_queue_strip_and_queue_label():
    d = pathlib.Path(web_server.__file__).parent
    js = (d / "app.js").read_text()
    for needle in ("case 'queued'", "case 'queue_cleared'", "function paintQueue()", "dequeue(ev.text)", "b ? 'Queue' : 'Send'"):
        assert needle in js, needle
    assert "$('go').disabled = b" not in js                                     # Send stays usable while a turn runs
    for name in ("index.html", "index_paper.html", "index_v2.html"):
        assert 'id="queue"' in (d / name).read_text(), name


def test_r374_the_paper_page_moved_to_paper_and_the_desktop_page_to_v1(app):
    assert "flavour 2 (Paper)" in _get(app, "/paper")[2] and "flavour 2 (Paper)" in _get(app, "/paper/")[2]
    assert "flavour 1 (Desktop)" in _get(app, "/v1")[2] and "flavour 1 (Desktop)" in _get(app, "/v1/")[2]


def test_r375_v2_drawer_key_is_real_code_not_part_of_a_comment():
    v2 = (pathlib.Path(web_server.__file__).parent / "index_paper.html").read_text()
    line = next(l for l in v2.splitlines() if "aurora_web_sidebar_v2" in l)
    assert line.lstrip().startswith("window.AURORA_SB_KEY") and "//" not in line.split("AURORA_SB_KEY")[0]


def test_r375_send_does_not_mark_busy_itself():
    js = (pathlib.Path(web_server.__file__).parent / "app.js").read_text()
    sub = js.split("$('send').onsubmit")[1].split("};")[0]
    assert "setBusy(true)" not in sub


def test_r375_a_fresh_page_replays_from_the_newest_reset(app):
    s = app.s
    s.emit("text", text="old")
    s.new_chat()
    reset = next(e["id"] for e in reversed(s.events) if e["type"] == "reset")
    evs = s.wait_events(0, 0.01)
    assert evs[0]["id"] == reset and all(e["type"] != "text" for e in evs)
    assert [e["id"] for e in s.wait_events(reset, 0.01)] == [e["id"] for e in s.events if e["id"] > reset]


def test_r375_a_page_ahead_of_a_restarted_server_gets_a_restart_marker_and_the_whole_log(app):
    s = app.s
    evs = s.wait_events(10_000, 0.01)
    assert evs[0]["type"] == "restart" and [e["id"] for e in evs[1:]] == [e["id"] for e in s.events]


def test_r376_resume_makes_a_past_session_live_and_draws_its_history(app):
    call(app, "/api/send", {"text": "remember the word kiwi"})
    assert wait_for(lambda: "done" in types(app))
    old = call(app, "/api/state")[1]["session_id"]
    from aurora.session import Session
    app.e.reset(); app.e.session = Session()          # a different live session, as after a restart
    assert call(app, "/api/state")[1]["session_id"] != old
    code, d = call(app, "/api/resume", {"id": old})
    assert code == 200, d
    assert call(app, "/api/state")[1]["session_id"] == old
    hist = next(e for e in reversed(app.s.events) if e["type"] == "history")
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant"] and "kiwi" in hist["messages"][0]["text"]
    assert app.e.messages[0]["content"].startswith("remember the word kiwi")


def test_r376_resume_refuses_bad_ids_and_a_running_turn(app):
    assert call(app, "/api/resume", {"id": "../../etc"})[0] == 400
    assert call(app, "/api/resume", {})[0] == 400
    assert call(app, "/api/resume", {"id": "x"}, token="")[0] == 401
    app.s.busy = True
    sid = app.s.engine.session.id
    from aurora import session as sessions
    if sid in sessions._base_session_ids():
        assert call(app, "/api/resume", {"id": sid})[0] == 409
    app.s.busy = False


def test_r376_diff_and_undo_preview_and_undo(app, tmp_path, monkeypatch):
    from aurora import rewind
    ws = tmp_path / "ws"; ws.mkdir(); monkeypatch.chdir(ws)
    f = ws / "a.txt"; f.write_text("one\n")
    app.e.last_turn_diff_base = None
    code, d = call(app, "/api/diff")
    assert code == 200 and d["diff"] == "" and d["note"]
    rewind.snapshot_before_write(str(f))
    app.e.last_turn_diff_base = rewind.head(".")
    f.write_text("two\n")
    rewind.checkpoint("[write_file] change a.txt")
    code, d = call(app, "/api/diff")
    assert code == 200 and "+two" in d["diff"]
    code, p = call(app, "/api/undo")
    assert code == 200 and p["kind"] != "none" and "two" in p["diff"]
    app.s.busy = True
    assert call(app, "/api/undo", {})[0] == 409
    app.s.busy = False
    code, r = call(app, "/api/undo", {})
    assert code == 200 and r["ok"], r
    assert f.read_text() == "one\n"
    assert call(app, "/api/undo", token="")[0] == 401


def test_r376_page_has_resume_diff_and_undo_controls():
    d = pathlib.Path(web_server.__file__).parent
    js = (d / "app.js").read_text()
    for name in ("index.html", "index_paper.html", "index_v2.html"):
        html = (d / name).read_text()
        for i in ("view-resume", "diff-btn", "undo-btn", "cf-title", "cf-text", "cf-pre"):
            assert f'id="{i}"' in html, (name, i)
    assert "'/api/resume'" in js and "'/api/undo'" in js and "'/api/diff'" in js and "case 'history'" in js


def test_r377_the_chat_page_is_the_default(app):
    st, ct, body = _get(app, "/")
    assert st == 200 and "flavour 3 (Chat)" in body and "window.AURORA_CHAT = true" in body
    for path in ("/v2", "/v2/"):                                        # R385: the route is gone
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(app, path)
        assert err.value.code == 404


def test_r377_chat_page_layout_and_phone_rules():
    d = pathlib.Path(web_server.__file__).parent
    v2, js = (d / "index_v2.html").read_text(), (d / "app.js").read_text()
    for i in ("side", "new-chat", "search", "sessions", "chat-title", "settings-btn", "settings", "jump", "sb-close", "scrim"):
        assert f'id="{i}"' in v2, i
    assert "interactive-widget=resizes-content" in v2 and "env(safe-area-inset-bottom)" in v2
    phone = "".join(v2.split("@media (max-width:768px){")[1:])   # R417: there are several phone blocks
    assert "#side{position:fixed" in phone   # R431: Settings is a panel, no phone sheet
    assert "localStorage.removeItem(window.AURORA_SB_KEY)" in v2           # a phone opens on the chat
    assert 'id="theme-sel"' in v2 and 'value="auto"' in v2 and 'value="light"' in v2 and 'value="dark"' in v2      # R390: a select now
    assert "if (window.AURORA_CHAT) return chatList();" in js and "'/api/search?q='" in js and "'/api/resume'" in js


def test_r377_search_finds_sessions_by_their_text(app):
    call(app, "/api/send", {"text": "the zebra question"})
    assert wait_for(lambda: "done" in types(app))
    code, d = call(app, "/api/search?q=zebra")
    assert code == 200 and d["sessions"] and d["sessions"][0]["current"] and "zebra" in d["sessions"][0]["first"]
    assert call(app, "/api/search?q=z")[1]["sessions"] == []               # one letter is not a search
    assert call(app, "/api/search?q=zebra", token="")[0] == 401


def test_r377_new_chat_after_turns_starts_a_new_session_and_keeps_the_old_one(app):
    first = call(app, "/api/state")[1]["session_id"]
    assert call(app, "/api/new", {})[0] == 200
    assert call(app, "/api/state")[1]["session_id"] == first              # nothing said yet: same session
    call(app, "/api/send", {"text": "hello"})
    assert wait_for(lambda: "done" in types(app))
    assert call(app, "/api/new", {})[0] == 200
    second = call(app, "/api/state")[1]["session_id"]
    assert second != first
    ids = [x["id"] for x in call(app, "/api/sessions?limit=50")[1]["sessions"]]
    assert first in ids
    assert call(app, "/api/sessions?limit=abc")[0] == 200


def test_r378_the_logo_smiles_below_its_eyes():
    d = pathlib.Path(web_server.__file__).parent
    for name in ("index_v2.html",):                                 # R398/R399: the face is only v2's installed-app sidebar mark now
        html = d.joinpath(name).read_text()
        assert "M10.5 13c2.6" not in html, name                       # the old smile, drawn above the eyes
        smile = re.search(r"M13\.9 ([\d.]+)c[^']*'([^/]*)/", html)
        smile_y = float(smile.group(1))
        assert "dasharray" not in smile.group(2), name                 # one soft curve, not split into dashes
        eyes_end = max(float(a) + float(b) for a, b in re.findall(r"M(?:12|20)\.[46] ([\d.]+)v([\d.]+)", html))
        assert eyes_end + 1.5 < smile_y < 22, name                     # under the eyes, clear of the outline


def test_r379_v2_browser_tab_uses_the_site_logo_but_the_installed_app_keeps_the_face():
    html = (pathlib.Path(web_server.__file__).parent / "index_v2.html").read_text()
    m = re.search(r"@media \(display-mode: browser\)\{\.brand \.logo,\.msg\.reply::before\{background:#000 url\(\"data:image/webp;base64,([A-Za-z0-9+/=]+)\"\)", html)
    assert m
    raw = base64.b64decode(m.group(1))
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    assert "data:image/svg+xml" in html                           # the face stays the default


def test_r380_reply_avatar_override_comes_after_the_face_rule_and_files_drop_on_the_message_bar():
    d = pathlib.Path(web_server.__file__).parent
    v2, js = (d / "index_v2.html").read_text(), (d / "app.js").read_text()
    assert v2.index("@media (display-mode: browser){") > v2.index(".msg.reply::before{content")     # same specificity: the later rule wins
    assert "addEventListener('drop'" in js and "/api/attach" in js


def _zip_bytes(files):
    import io
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    return b.getvalue()


def test_r381_documents_become_text_and_junk_is_refused():
    from aurora.web import attach
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    docx = _zip_bytes({"word/document.xml": f'<w:document {ns}><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p><w:p><w:r><w:t>World</w:t></w:r></w:p></w:body></w:document>'})
    assert attach.process("a.docx", docx)["text"] == "Hello\nWorld"
    xl = _zip_bytes({"xl/sharedStrings.xml": '<sst xmlns="x"><si><t>name</t></si></sst>',
                     "xl/workbook.xml": '<workbook xmlns="x"><sheets><sheet name="S1"/></sheets></workbook>',
                     "xl/worksheets/sheet1.xml": '<worksheet xmlns="x"><sheetData><row><c t="s"><v>0</v></c><c><v>42</v></c></row></sheetData></worksheet>'})
    assert "name\t42" in attach.process("b.xlsx", xl)["text"]
    pp = _zip_bytes({"ppt/slides/slide1.xml": '<p:sld xmlns:p="x" xmlns:a="y"><a:t>Slide text</a:t></p:sld>'})
    assert "Slide text" in attach.process("c.pptx", pp)["text"]
    assert attach.process("d.py", b"print(1)\n")["text"] == "print(1)\n"          # R405: plain text is kept raw
    big = attach.process("e.txt", b"x" * (attach.MAX_TEXT + 5))
    assert big["cut"] and len(big["text"]) == attach.MAX_TEXT
    for name, data in (("f.bin", b"\0\1\2"), ("g.docx", b"not a zip"), ("h.txt", b""), ("i.pdf", b"%PDF-broken")):
        with pytest.raises(attach.Unsupported):
            attach.process(name, data)


def test_r381_pictures_need_a_vision_model_and_reach_the_provider_as_image_parts(app):
    from aurora.providers.openai_compat import _with_image_parts
    from aurora.web import attach
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), "red").save(buf, "PNG")
    res = attach.process("big.png", buf.getvalue())
    assert res["kind"] == "image" and res["data_url"].startswith("data:image/")
    shrunk = Image.open(io.BytesIO(__import__("base64").b64decode(res["data_url"].split(",", 1)[1])))
    assert max(shrunk.size) == attach.MAX_SIDE
    m = {"role": "user", "content": "look", "images": ["data:image/png;base64,AAAA"]}
    out = _with_image_parts(m)
    assert out["content"][0] == {"type": "text", "text": "look"} and out["content"][1]["type"] == "image_url" and "images" not in out
    assert "images" in m                                         # the history copy is not mutated
    plain = {"role": "user", "content": "x"}
    assert _with_image_parts(plain) is plain


def test_r382_a_multimodal_server_counts_as_vision_without_the_config_flag():
    from aurora.providers.openai_compat import OpenAICompatProvider as P

    class R:
        def __init__(self, body): self.body = body
        def json(self): return self.body

    class C:
        def __init__(self, body): self.body = body
        def get(self, url, *a, **k):
            if isinstance(self.body, Exception):
                raise self.body
            return R(self.body if url.endswith("/models") or "modalities" not in str(self.body) else self.body)

    def probe(body):
        p = P.__new__(P)
        p.pick_endpoint = lambda cache_ok=True: "http://x/v1"
        p._client_for = lambda base: C(body)
        p._auth_headers = lambda: {}
        return p.serves_vision()

    assert probe({"models": [{"capabilities": ["completion", "multimodal"]}]}) is True
    assert probe({"modalities": {"vision": True, "audio": False}}) is True      # /props, what llama-server answers behind the gateway
    assert probe({"modalities": {"vision": False}}) is False
    assert probe({"models": [{"capabilities": ["completion"]}], "data": []}) is False
    assert probe(OSError("down")) is False


def _talk(app, text):
    call(app, "/api/send", {"text": text})
    assert wait_for(lambda: "done" in types(app))
    sid = call(app, "/api/state")[1]["session_id"]
    call(app, "/api/new", {})
    app.s.events.clear()
    return sid


def test_r383_delete_hides_the_chat_from_the_web_but_keeps_the_log(app):
    from aurora import session as sessions
    sid = _talk(app, "to be deleted")
    app.e.reset(); app.e.session = sessions.Session()
    assert any(x["id"] == sid for x in call(app, "/api/sessions")[1]["sessions"])
    assert call(app, "/api/chat/delete", {"id": sid})[0] == 200
    assert not any(x["id"] == sid for x in call(app, "/api/sessions")[1]["sessions"])
    assert not any(x["id"] == sid for x in call(app, "/api/search?q=deleted")[1]["sessions"])
    assert sid in sessions._base_session_ids()                          # the TUI still has it
    assert call(app, "/api/chat/delete", {"id": "nope"})[0] == 400
    assert call(app, "/api/chat/delete", {"id": sid}, token="")[0] == 401


def test_r383_the_open_chat_cannot_be_deleted(app):
    call(app, "/api/send", {"text": "live"})
    assert wait_for(lambda: "done" in types(app))
    code, d = call(app, "/api/chat/delete", {"id": call(app, "/api/state")[1]["session_id"]})
    assert code == 400 and "open" in d["error"]


def test_r383_folders_flat_one_chat_one_folder_and_deleting_one_hides_its_chats(app):
    from aurora import session as sessions
    a = _talk(app, "alpha chat")
    app.e.reset(); app.e.session = sessions.Session()
    b = _talk(app, "beta chat")
    app.e.reset(); app.e.session = sessions.Session()
    f1 = call(app, "/api/folder/create", {"name": "Work"})[1]["id"]
    f2 = call(app, "/api/folder/create", {"name": "Home"})[1]["id"]
    assert call(app, "/api/folder/create", {"name": "  "})[0] == 400
    assert call(app, "/api/chat/move", {"chat": a, "folder": f1})[0] == 200
    assert call(app, "/api/chat/move", {"chat": a, "folder": f2})[0] == 200      # moving, not copying
    assert call(app, "/api/chat/move", {"chat": b, "folder": "zzz"})[0] == 400
    d = call(app, "/api/sessions")[1]
    assert {f["name"] for f in d["folders"]} == {"Work", "Home"}
    assert {x["id"]: x["folder"] for x in d["sessions"]}[a] == f2
    assert call(app, "/api/folder/rename", {"id": f2, "name": "Play"})[0] == 200
    assert call(app, "/api/folder/open", {"id": f2, "open": False})[0] == 200
    assert next(f for f in call(app, "/api/sessions")[1]["folders"] if f["id"] == f2) == {"id": f2, "name": "Play", "open": False}
    assert call(app, "/api/chat/move", {"chat": a, "folder": ""})[0] == 200      # dragged back out
    assert {x["id"]: x["folder"] for x in call(app, "/api/sessions")[1]["sessions"]}[a] is None
    call(app, "/api/chat/move", {"chat": a, "folder": f2})
    assert call(app, "/api/folder/delete", {"id": f2})[0] == 200
    d = call(app, "/api/sessions")[1]
    assert f2 not in [f["id"] for f in d["folders"]] and a not in [x["id"] for x in d["sessions"]]
    assert b in [x["id"] for x in d["sessions"]] and a in sessions._base_session_ids()
    assert call(app, "/api/folder/delete", {"id": f2})[0] == 400
    assert call(app, "/api/folder/create", {"name": "x"}, token="")[0] == 401


def test_r383_the_store_survives_a_restart_and_a_corrupt_file(app):
    from aurora.web import folders
    fid, _ = folders.create("Keep")
    assert [f["name"] for f in folders.view()["folders"]] == ["Keep"]
    folders._file().write_text("{not json")
    assert folders.view() == {"hidden": [], "folders": [], "titles": {}}


def test_r383_page_has_delete_folders_and_mouse_only_drag():
    d = pathlib.Path(web_server.__file__).parent
    js, v2 = (d / "app.js").read_text(), (d / "index_v2.html").read_text()
    assert 'id="new-folder"' in v2
    for api in ("/api/chat/delete", "/api/chat/move", "/api/folder/create", "/api/folder/rename", "/api/folder/open", "/api/folder/delete"):
        assert api in js, api
    assert "(hover:hover) and (pointer:fine)" in js and "b.draggable = true" in js and "if (!q && FINE())" in js   # no drag on touch, none in search
    assert "(hover:none),(pointer:coarse)" in v2                                                                    # delete is always visible on a phone


def test_r383_folder_name_is_a_form_so_phone_enter_works():
    js = (pathlib.Path(web_server.__file__).parent / "app.js").read_text()
    body = js.split("function nameInput(")[1].split("async function deleteChat")[0]
    assert "createElement('form')" in body and "f.onsubmit" in body and "i.onblur = () => end(true)" in body


def test_r384_done_carries_the_model_loaded_after_the_turn(app):
    import time
    app.e._live_name_cache[app.e._provider_key] = (None, time.time())   # stale "asleep" answer, fresh enough to be served
    app.state["prov"].live_model_name = lambda: "models/woke.gguf"
    call(app, "/api/send", {"text": "hello"})
    assert wait_for(lambda: "state" in types(app))          # R392 (bug 16): the probe follows `done` as a `state` event
    st = [ev for ev in app.s.events if ev["type"] == "state"][-1]
    assert st["context"]["live_model"] == "models/woke.gguf"
    assert types(app).index("done") < types(app).index("state")


def test_r385_sidebar_rows_carry_the_number_of_messages_sent_and_a_renamable_title(app):
    from aurora import session as sessions
    sid = _talk(app, "count me")
    app.e.reset(); app.e.session = sessions.Session()
    row = next(x for x in call(app, "/api/sessions")[1]["sessions"] if x["id"] == sid)
    assert row["prompts"] == 1 and row["renamed"] is False and row["first"] == "count me"
    assert call(app, "/api/chat/rename", {"id": sid, "name": "  My   title "})[0] == 200
    row = next(x for x in call(app, "/api/sessions")[1]["sessions"] if x["id"] == sid)
    assert row["first"] == "My title" and row["renamed"] is True and row["prompts"] == 1
    assert next(x for x in call(app, "/api/search?q=count")[1]["sessions"] if x["id"] == sid)["first"] == "My title"
    assert call(app, "/api/chat/rename", {"id": "nope", "name": "x"})[0] == 400
    assert call(app, "/api/chat/rename", {"id": sid, "name": "x"}, token="")[0] == 401
    assert call(app, "/api/chat/rename", {"id": sid, "name": ""})[0] == 200          # empty: back to the first message
    assert next(x for x in call(app, "/api/sessions")[1]["sessions"] if x["id"] == sid)["first"] == "count me"


def test_r386_chat_header_always_shows_context_bar_tokens_and_compactions():
    d = pathlib.Path(web_server.__file__).parent
    js, chat = (d / "app.js").read_text(), (d / "index_v2.html").read_text()
    head = chat.split("</header>")[0]
    assert 'id="hmeter"' in head and 'id="ctx-txt"' in head and 'id="ctx-fold"' in head
    assert "fmtTok(" in js and "c.compactions" in js and "G('ctx-fold')" in js
    assert '"compactions": getattr(st, "compactions", 0)' in (d / "core.py").read_text()


def test_r387_model_menu_lists_switches_and_remembers_for_the_page_only(app, monkeypatch):
    code, d = call(app, "/api/models")
    names = [m["model"] for m in d["models"]]
    assert code == 200 and names == ["other", "local"] and [m["current"] for m in d["models"]] == [False, True]
    assert call(app, "/api/model", {"i": 0})[0] == 200
    assert app.e.current["model"] == "other" and call(app, "/api/state")[1]["model"] == "other"
    from aurora import config
    st = config.load_state()
    assert st.get("web_last_model") == "other" and st.get("last_model") != "other"        # the TUI's remembered model is untouched
    for bad in (7, -1, "0", True, None):
        assert call(app, "/api/model", {"i": bad})[0] == 400
    assert call(app, "/api/model", {"i": 0}, token="")[0] == 401
    monkeypatch.setenv("AURORA_WEB_TOKEN", TOKEN)
    app.e.current = next(m for m in app.e.models if m["model"] == "local")
    monkeypatch.setattr(web_server, "Server",
                        lambda *a, **k: type("H", (), {"serve_forever": lambda s: None, "daemon_threads": 0})())
    cwd = pathlib.Path.cwd()
    try:
        web_server.serve(app.e, "127.0.0.1", 0, str(app.tmp))
    finally:
        import os
        os.chdir(cwd)
    assert app.e.current["model"] == "other"                                             # restart: the page's own pick comes back


def test_r387_switch_refused_while_a_turn_runs_or_the_key_is_missing(app, monkeypatch):
    app.s.busy = True
    code, d = call(app, "/api/model", {"i": 0})
    app.s.busy = False
    assert code == 409 and app.e.current["model"] == "local"
    monkeypatch.setattr(app.e, "_has_key", lambda pkey: False)
    code, d = call(app, "/api/model", {"i": 0})
    assert code == 400 and "no API key" in d["error"] and app.e.current["model"] == "local"
    assert all(m["ready"] is False for m in call(app, "/api/models")[1]["models"])


def test_r387_model_tag_is_a_button_that_opens_the_menu():
    d = pathlib.Path(web_server.__file__).parent
    js = (d / "app.js").read_text()
    assert "'/api/models'" in js and "'/api/model'" in js and "model-menu" in js and "setAttribute('role', 'button')" in js
    assert "m.model === 'local' ? m.provider" in js and "s.provider + ' · '" in js


def test_r388_page_can_open_on_the_last_chat():
    d = pathlib.Path(web_server.__file__).parent
    js = (d / "app.js").read_text()
    assert "else if (!s.busy) await continueLast();" in js
    assert "x.current && x.prompts > 0" in js and "'/api/resume'" in js
    for page in ("index.html", "index_v2.html", "index_paper.html"):
        assert "New chat on open" in (d / page).read_text()


def test_r389_notes_create_save_list_read_delete_and_survive_bad_input(app):
    code, d = call(app, "/api/notes/create", {"title": "  Shopping \n list ", "text": "milk\neggs"})
    nid = d["id"]
    assert code == 200 and len(nid) == 12
    first = call(app, "/api/notes")[1]["notes"][0]
    assert first == {"id": nid, "title": "Shopping list", "preview": "milk eggs", "updated": first["updated"], "type": "personal"}   # R422: every note has a type
    assert call(app, "/api/notes/" + nid)[1]["text"] == "milk\neggs"
    assert (app.tmp / "home" / "web-notes" / f"{nid}.txt").read_text() == "milk\neggs"           # plain .txt on disk
    assert call(app, "/api/notes/save", {"id": nid, "title": "", "text": "x"})[0] == 200
    assert call(app, "/api/notes")[1]["notes"][0]["title"] == ""                          # blank stays blank; the page shows "Untitled"
    assert [n["id"] for n in call(app, "/api/notes?q=X")[1]["notes"]] == [nid]             # full-text, case-insensitive
    assert call(app, "/api/notes?q=milk")[1]["notes"] == [] and call(app, "/api/notes?q=")[1]["notes"][0]["id"] == nid
    assert call(app, "/api/notes/save", {"id": "nope", "title": "t", "text": "x"})[0] == 404   # R392: the page offers "save as new"
    assert call(app, "/api/notes/save", {"id": nid, "title": "t", "text": "x" * 100_001})[0] == 400
    for bad in ("../../etc/passwd", "ABC", "zzzzzzzzzzzz"):
        assert call(app, "/api/notes/" + bad)[0] == 404
        assert call(app, "/api/notes/delete", {"id": bad})[0] == 400
    assert call(app, "/api/notes", token="")[0] == 401 and call(app, "/api/notes/create", {}, token="")[0] == 401
    assert call(app, "/api/notes/delete", {"id": nid})[0] == 200
    assert call(app, "/api/notes")[1]["notes"] == [] and not (app.tmp / "home" / "web-notes" / f"{nid}.txt").exists()


def test_r390_menu_is_dropdowns_and_commands_sit_beside_send():
    d = pathlib.Path(web_server.__file__).parent
    page = (d / "index_v2.html").read_text()
    assert 'id="theme-sel"' in page and 'id="opt-dd"' not in page and 'id="md-on"' in page and "data-theme-set" not in page
    assert 'data-fold="commands"' not in page                                     # no second copy in the Menu
    crow = page.split('class="crow"')[1].split("</form>")[0]
    assert 'id="cmd-btn"' in crow and crow.index('id="cmd-btn"') < crow.index('id="go"')
    menu = page.split('id="cmd-menu"')[1].split("</div>")[0]
    for cmd in ('data-cmd="compact"', 'data-cmd="context"', 'id="diff-btn"', 'id="undo-btn"', 'id="load-ctx"', 'data-cmd="clear"'):
        assert cmd in menu
    js = (d / "app.js").read_text()
    assert "$('cmd-btn')" in js and "$('notes-btn')" in js and 'id="notes-btn"' in page and '<dialog id="notes"' in page


def test_r389_notes_page_autosaves_searches_and_has_no_manual_save_or_native_confirm():
    d = pathlib.Path(web_server.__file__).parent
    page, js = (d / "index_v2.html").read_text(), (d / "app.js").read_text()
    notes = js[js.index("// R389: Notes"):]
    assert 'id="note-save"' not in page and 'id="note-search"' in page and 'id="note-back"' in page   # autosave; search; phone list -> note
    assert "setTimeout(flush, 700)" in notes and "keepalive" in notes and "pagehide" in notes          # saves on a pause, on close and on leaving
    assert "confirm(" not in notes.replace("askConfirm(", "")                                          # the page's own dialog, never the browser's


def test_r391_auto_approve_lives_in_the_menu_not_beside_the_model_name():
    page = (pathlib.Path(web_server.__file__).parent / "index_v2.html").read_text()
    crow = page.split('class="crow"')[1].split("</form>")[0]
    settings = page.split('id="settings"')[1]
    assert 'id="auto-on"' not in crow and 'id="auto-tag"' not in crow
    assert 'id="auto-on"' in settings and settings.index('id="auto-on"') < settings.index('id="clear-open"')
