"""R392 — Aurora Web review fixes (2026-11-02): reliability, speed, approval policy, plan/usage/retry, /health."""
import http.client
import json
import socket
import threading
import time
import urllib.request

import pytest

from aurora.providers.base import ToolCall, TurnResult
from aurora.web import core as web_core
from aurora.web import folders, notes
from aurora.web import server as web_server
from aurora.web.core import WebFrontend, WebSession
from tests.test_web import app, call, types, wait_for  # noqa: F401  (fixture)


def _gate(app):
    gate = threading.Event()
    prov = app.state["prov"]
    orig = prov.turn
    prov.turn = lambda *a, **k: (gate.wait(3), orig(*a, **k))[1]
    return gate


# ── reliability ──────────────────────────────────────────────────────────
def test_bug1_stop_does_not_cancel_the_message_queued_after_it(app):
    gate = _gate(app)
    app.state["prov"].results = [TurnResult(text="a", stop_reason="end"), TurnResult(text="b", stop_reason="end")] * 3
    assert call(app, "/api/send", {"text": "first"})[0] == 200
    assert call(app, "/api/stop", {})[0] == 200
    assert call(app, "/api/send", {"text": "after stop"})[0] == 200      # queued behind the stopped turn
    gate.set()
    assert wait_for(lambda: types(app).count("done") >= 2)
    assert app.s._stop is False


def test_bug2_an_oversized_body_closes_the_keepalive_connection(app):
    host, port = app.base.split("//")[1].split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=5)
    c.request("POST", "/api/notes/create", body=b"x" * (web_server.MAX_BODY + 10),
              headers={"Authorization": "Bearer t0k", "Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 413 and r.getheader("Connection", "").lower() == "close"
    assert "too large" in json.loads(r.read())["error"]


def test_bug3_a_full_size_note_in_two_byte_characters_saves(app):
    nid = call(app, "/api/notes/create", {"title": "t", "text": ""})[1]["id"]
    code, d = call(app, "/api/notes/save", {"id": nid, "title": "t", "text": "é" * notes.MAX_TEXT})
    assert code == 200, d


def test_bug3_an_oversized_send_says_too_large_not_empty(app):
    code, d = call(app, "/api/send", {"text": "x", "images": ["data:image/png;base64," + "A" * web_server.MAX_SEND_BODY]})
    assert code == 413 and "too large" in d["error"]


def test_bug7_a_fresh_page_never_starts_inside_a_reply(app, monkeypatch):
    monkeypatch.setattr(web_core, "MAX_EVENTS", 10)
    s = app.s
    s.emit("user", text="one")
    for i in range(12):
        s.emit("text", text=str(i))
    s.emit("done")
    s.emit("user", text="two")
    s.emit("text", text="x")
    evs = s.wait_events(0, 0.01)
    assert evs[0]["type"] == "user" and evs[0]["text"] == "two"


def test_bug8_new_chat_holds_the_turn_slot(app):
    assert app.s._claim() and app.s.busy
    assert call(app, "/api/new", {})[0] == 409 and call(app, "/api/undo", {})[0] == 409
    app.s._release()
    assert call(app, "/api/new", {})[0] == 200 and app.s.busy is False


def test_bug10_small_rotated_photo_is_reencoded_upright():
    pytest.importorskip("PIL")
    import base64
    import io
    from PIL import Image
    from aurora.web import attach
    im = Image.new("RGB", (40, 20), "red")
    ex = im.getexif()
    ex[0x0112] = 6                                     # rotate 90° on display
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=ex.tobytes())
    url = attach.process("p.jpg", buf.getvalue())["data_url"]
    out = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert out.size == (20, 40)


def test_bug11_a_page_from_another_server_run_gets_restart_not_a_splice(app):
    s = app.s
    for i in range(5):
        s.emit("text", text=str(i))
    evs = s.wait_events(2, 0.01, boot="not-this-run")
    assert evs[0]["type"] == "restart" and evs[0]["boot"] == s.boot
    assert [e["id"] for e in evs[1:]] == [e["id"] for e in s.events]
    assert s.wait_events(2, 0.01, boot=s.boot)[0]["id"] == 3        # same run: plain continuation


def test_p10_restart_replays_from_the_newest_reset_only(app):
    s = app.s
    s.emit("text", text="old")
    s.emit("reset")
    s.emit("user", text="new")
    evs = s.wait_events(10_000, 0.01)
    assert [e["type"] for e in evs] == ["restart", "reset", "user"]


def test_bug12_a_damaged_notes_index_is_moved_aside_and_rebuilt(app):
    a = notes.create("A", "alpha")[0]
    notes.create("B", "beta")
    (notes._dir() / "index.json").write_text("{trunc")
    ids = {n["id"] for n in notes.listing()}
    assert a in ids and len(ids) == 2                                  # rebuilt from the .txt files
    assert list(notes._dir().glob("index.json.corrupt-*")) and "damaged" in notes.problem
    notes.create("C", "gamma")
    assert len(notes.listing()) == 3


def test_bug12_a_damaged_folder_store_is_kept_aside(app):
    folders.create("Keep")
    folders._file().write_text("{bad")
    folders.create("New")
    assert list(folders._file().parent.glob("web-chats.json.corrupt-*"))
    assert "{bad" == next(folders._file().parent.glob("web-chats.json.corrupt-*")).read_text()


def test_bug13_save_with_a_stale_base_is_a_conflict(app):
    nid = call(app, "/api/notes/create", {"title": "t", "text": "a"})[1]["id"]
    base = call(app, "/api/notes/" + nid)[1]["updated"]
    code, d = call(app, "/api/notes/save", {"id": nid, "title": "t", "text": "desk", "base": base})
    assert code == 200 and d["updated"] > base
    code, d = call(app, "/api/notes/save", {"id": nid, "title": "t", "text": "phone", "base": base})
    assert code == 409 and d["error"] == notes.CONFLICT
    assert call(app, "/api/notes/" + nid)[1]["text"] == "desk"
    assert call(app, "/api/notes/save", {"id": nid, "title": "t", "text": "forced"})[0] == 200    # no base = overwrite


def test_bug15_auto_approve_is_remembered_across_restarts(app):
    assert call(app, "/api/auto", {"on": False})[0] == 200
    assert WebSession(app.e).auto is False
    assert call(app, "/api/auto", {"on": True})[0] == 200
    assert WebSession(app.e).auto is True


def test_bug16_done_is_sent_before_the_model_probe(app, monkeypatch):
    slow = threading.Event()
    monkeypatch.setattr(app.e, "refresh_live_state", lambda: slow.wait(2))
    t0 = time.time()
    call(app, "/api/send", {"text": "hi"})
    assert wait_for(lambda: "done" in types(app)) and time.time() - t0 < 1.5
    slow.set()
    assert wait_for(lambda: "state" in types(app))


def test_bug17_the_vision_probe_is_cached(app):
    n = {"k": 0}
    app.state["prov"].serves_vision = lambda: (n.__setitem__("k", n["k"] + 1), True)[1]
    assert app.s.vision(probe=True) and app.s.vision(probe=True) and app.s.vision()
    assert n["k"] == 1


# ── speed ───────────────────────────────────────────────────────────────
def test_p1_text_chunks_are_coalesced_and_keep_their_order(app):
    fe = WebFrontend(app.s)
    for ch in "hello world":
        fe.on_text(ch)
    fe.on_think("hm")
    fe.on_tool_start("read_file", {"path": "x"})
    fe.flush()
    evs = [e for e in app.s.events if e["type"] in ("text", "think", "tool_start")]
    assert [e["type"] for e in evs] == ["text", "think", "tool_start"] and evs[0]["text"] == "hello world"


def test_p1_a_lone_chunk_is_sent_after_the_short_delay(app):
    fe = WebFrontend(app.s)
    fe.on_text("x")
    assert wait_for(lambda: "text" in types(app), 1)


def test_p7_the_replay_buffer_is_capped_by_bytes(app, monkeypatch):
    monkeypatch.setattr(web_core, "MAX_EVENT_BYTES", 50_000)
    for _ in range(20):
        app.s.emit("tool_result", name="x", output="y" * 10_000)
    assert app.s._bytes <= 60_000 and len(app.s.events) < 20


def test_p8_listen_backlog_is_large():
    assert web_server.Server.request_queue_size >= 128


def test_p12_streams_are_capped(app, monkeypatch):
    monkeypatch.setattr(web_server, "MAX_STREAMS", 0)
    req = urllib.request.Request(app.base + "/api/stream?after=0", headers={"Authorization": "Bearer t0k"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=3)
    assert e.value.code == 429


def test_p11_html_pins_a_cacheable_script_and_refuses_framing(app):
    with urllib.request.urlopen(app.base + "/", timeout=5) as r:
        html = r.read().decode()
        assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    v = web_server._script_version()
    assert f'/app.js?v={v}' in html
    with urllib.request.urlopen(app.base + f"/app.js?v={v}", timeout=5) as r:
        assert "immutable" in r.headers["Cache-Control"]
    with urllib.request.urlopen(app.base + "/app.js", timeout=5) as r:
        assert r.headers["Cache-Control"] == "no-store"


def test_p4_p5_session_listing_and_search_are_cached_by_mtime(app, monkeypatch):
    from aurora import session as sessions
    call(app, "/api/send", {"text": "find the kiwi"})
    assert wait_for(lambda: "done" in types(app))
    assert sessions.list_sessions(5) and sessions.search_sessions("kiwi")
    calls = {"n": 0}
    orig = sessions.Session.iter_records
    monkeypatch.setattr(sessions.Session, "iter_records", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), orig(*a, **k))[1])
    real_open = open
    opened = {"n": 0}
    monkeypatch.setattr("builtins.open", lambda *a, **k: (opened.__setitem__("n", opened["n"] + 1), real_open(*a, **k))[1])
    sessions.list_sessions(5)
    sessions.search_sessions("kiwi")
    monkeypatch.setattr("builtins.open", real_open)
    assert calls["n"] == 0 and opened["n"] == 0


# ── approval policy ─────────────────────────────────────────────────────
def test_web_denylist_wins_even_with_auto_approve_on(app):
    app.s.auto = True
    fe = WebFrontend(app.s)
    ans = fe.approve("run_command", {"command": "sudo rm -rf /tmp/x"}, "")
    assert ans[0] == "n" and "denylist" in ans[1]
    assert fe.approve("run_command", {"command": "ls -la"}, "") == "y"
    assert web_core.web_denied("run_command", {"command": "git push origin main"})
    assert not web_core.web_denied("run_command", {"command": "git status"})


def test_web_denylist_file_overrides_the_default(app):
    from aurora.paths import aurora_home
    (aurora_home()).mkdir(parents=True, exist_ok=True)
    (aurora_home() / "web-denylist.txt").write_text("# mine\n\\bcowsay\\b\n")
    assert web_core.web_denied("run_command", {"command": "cowsay hi"})
    assert not web_core.web_denied("run_command", {"command": "sudo ls"})


def test_auto_off_asks_the_page(app):
    app.s.auto = False
    fe = WebFrontend(app.s)
    threading.Timer(0.1, lambda: app.s.answer(None, "y")).start()
    assert fe.approve("write_file", {"path": "a"}, "diff") == "y"
    assert "approve" in types(app)


def test_the_engine_never_auto_approves_on_its_own(app):
    WebSession(app.e, auto_approve=True)
    assert app.e.auto_approve is False


# ── features ────────────────────────────────────────────────────────────
def test_plan_mode_toggle(app):
    assert call(app, "/api/plan", {"on": "x"})[0] == 400
    assert call(app, "/api/plan", {"on": True})[0] == 200 and app.e.plan_mode is True
    assert call(app, "/api/state")[1]["plan_mode"] is True and "plan" in types(app)
    assert call(app, "/api/plan", {"on": False})[0] == 200 and app.e.plan_mode is False


def test_usage_and_budget(app):
    call(app, "/api/send", {"text": "hi"})
    assert wait_for(lambda: "done" in types(app))
    code, u = call(app, "/api/usage")
    assert code == 200 and {"models", "session_cost", "session_budget", "daily_budget"} <= set(u)
    assert call(app, "/api/budget", {"which": "session", "usd": 2.5})[0] == 200 and app.e.session_budget == 2.5
    assert call(app, "/api/budget", {"which": "session", "usd": -1})[0] == 400
    assert call(app, "/api/budget", {"which": "x", "usd": 1})[0] == 400


def test_retry_regenerates_and_edit_replaces_the_last_message(app):
    app.state["prov"].results = [TurnResult(text=t, stop_reason="end") for t in ("one", "two", "three")]
    call(app, "/api/send", {"text": "hello"})
    assert wait_for(lambda: "done" in types(app))
    assert call(app, "/api/retry", {})[0] == 200
    assert wait_for(lambda: types(app).count("done") >= 2)
    users = [m for m in app.e.messages if m["role"] == "user"]
    assert len(users) == 1 and users[0]["content"] == "hello"
    assert call(app, "/api/retry", {"text": "hello again"})[0] == 200
    assert wait_for(lambda: types(app).count("done") >= 3)
    users = [m for m in app.e.messages if m["role"] == "user"]
    assert [u["content"] for u in users] == ["hello again"]
    assert call(app, "/api/retry", {"text": "/model x"})[0] == 400


def test_retry_with_nothing_to_retry(app):
    assert call(app, "/api/retry", {})[0] == 400 and app.s.busy is False


def test_health_needs_no_token_and_reports_stores(app):
    with urllib.request.urlopen(app.base + "/health", timeout=5) as r:
        h = json.loads(r.read())
    assert {"ok", "version", "packages", "stores", "model_reachable"} <= set(h)
    assert h["stores"] == {"notes": True, "folders": True}


def test_transcribe_without_a_configured_service_is_501(app, monkeypatch):
    monkeypatch.delenv("AURORA_WEB_STT_URL", raising=False)
    req = urllib.request.Request(app.base + "/api/transcribe", data=b"abc", method="POST",
                                 headers={"Authorization": "Bearer t0k"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 501


def test_state_reports_activity_for_new_chat_on_open(app):
    st = call(app, "/api/state")[1]
    assert st["idle_s"] is None and "streams" in st and st["boot"] == app.s.boot
    call(app, "/api/send", {"text": "hi"})
    assert wait_for(lambda: "done" in types(app))
    assert call(app, "/api/state")[1]["idle_s"] < 5


def test_r393_web_defaults_assume_no_machine():
    import pathlib
    import re
    src = (pathlib.Path(web_server.__file__).parents[1] / "__main__.py").read_text()
    assert '"--workspace": "."' in src
    root = pathlib.Path(web_server.__file__).parents[2]
    units = sorted(p.name for p in (root / "deploy").iterdir() if p.suffix == ".service")
    assert units == [], "site units do not belong in the product"
    assert re.search(r"YOUR_USER", (root / "deploy" / "aurora-web.service.example").read_text())


def test_r393_non_loopback_bind_warns(app, monkeypatch, capsys):
    import os
    monkeypatch.setenv("AURORA_WEB_TOKEN", "t0k")
    monkeypatch.setattr(web_server, "Server", lambda *a, **k: type("H", (), {"serve_forever": lambda s: None})())
    cwd = os.getcwd()
    try:
        web_server.serve(app.e, "0.0.0.0", 0, str(app.tmp))
    finally:
        os.chdir(cwd)
    assert "WARNING" in capsys.readouterr().err


def test_r393_version_flag():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "aurora", "--version"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.startswith("aurora ")
