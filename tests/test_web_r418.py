"""R418: the web features from card 20261103-104015-auro-web-issues (F1-F5, I1-I5)."""
import pathlib
import urllib.request

from aurora.providers.base import TurnResult
from tests.test_core import FakeProvider
from tests.test_web import TOKEN, app, call, types, wait_for  # noqa: F401  (fixture)
from tests.test_web_r401 import _seen

WEB = pathlib.Path(__file__).resolve().parents[1] / "aurora" / "web"


def _turn(app, text="hello"):
    n = len([e for e in app.s.events if e["type"] == "done"])
    assert app.s.send(text)[0]
    assert wait_for(lambda: len([e for e in app.s.events if e["type"] == "done"]) > n and not app.s.busy)
    return [e for e in app.s.events if e["type"] == "done"][-1]


def _get(app, path, token=None):
    req = urllib.request.Request(app.base + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as ex:
        return ex.code, dict(ex.headers), ex.read().decode()


# F1 share links
def test_r418_f1_share_link_reads_without_token_and_revokes(app):
    _turn(app, "the <b>secret</b> plan")
    sid = app.e.session.id
    code, d = call(app, "/api/share", {"id": sid})
    assert code == 200 and d["path"].startswith("/shared/") and len(d["token"]) == 22
    assert call(app, "/api/share", {"id": sid})[1]["token"] == d["token"]          # one link per chat
    code, h, body = _get(app, d["path"])
    assert code == 200 and "the &lt;b&gt;secret&lt;/b&gt; plan" in body and "<script" not in body
    assert "default-src 'none'" in h["Content-Security-Policy"] and "noindex" in h["X-Robots-Tag"]
    assert call(app, "/api/share/delete", {"token": d["token"]})[0] == 200
    assert _get(app, d["path"])[0] == 404


def test_r418_f1_bad_tokens_and_ids(app):
    assert _get(app, "/shared/../../etc/passwd")[0] == 404
    assert _get(app, "/shared/" + "A" * 22)[0] == 404
    assert call(app, "/api/share", {"id": "nope"})[0] == 400
    assert call(app, "/api/share", {"id": "x"}, token="")[0] == 401


# F2 per-turn line
def test_r418_f2_done_carries_this_turns_tokens_and_time(app):
    app.state["prov"] = FakeProvider([TurnResult(text="hi", stop_reason="end", input_tokens=1200, output_tokens=34)])
    t = _turn(app)["turn"]
    assert t["input"] == 1200 and t["output"] == 34 and t["secs"] >= 0 and "cost" in t


# F3 approval countdown
def test_r418_f3_approve_event_says_its_timeout_and_a_denial_is_announced(app):
    from tests.test_web import _write_turn
    _write_turn(app)
    assert call(app, "/api/send", {"text": "write it"})[0] == 200
    assert wait_for(lambda: "approve" in types(app))
    ev = next(e for e in app.s.events if e["type"] == "approve")
    assert ev["timeout"] == app.s.approve_timeout
    assert wait_for(lambda: any(e["type"] == "notify" and "no answer within" in e["text"] for e in app.s.events))


def test_r418_f3_timeout_from_the_environment(monkeypatch):
    from aurora.web.core import WebSession
    monkeypatch.setenv("AURORA_WEB_APPROVE_TIMEOUT", "42")

    class E:
        session = type("S", (), {"id": "x"})()
    assert WebSession(E(), auto_approve=False).approve_timeout == 42


# F4 output files
def test_r418_f4_a_file_written_to_outputs_is_announced(app, monkeypatch):
    from aurora.web import attach
    d = attach.outputs_dir()
    real = app.e.send

    def send(text, fe, **k):
        (d / "report.csv").write_text("a,b\n")
        fe.on_tool_result("write_file", "ok")
        return real(text, fe, **k)
    monkeypatch.setattr(app.e, "send", send)
    _turn(app)
    outs = [e for e in app.s.events if e["type"] == "output"]
    assert len(outs) == 1 and outs[0]["name"] == "report.csv" and outs[0]["size"] == 4


# F5 temperature
def test_r418_f5_temperature_applies_to_one_turn_only(app):
    seen = []
    prov = app.state["prov"]
    orig = prov.turn

    def turn(*a, **k):
        seen.append(dict(prov.extra_body))
        return orig(*a, **k)
    prov.turn = turn
    assert call(app, "/api/send", {"text": "a", "temperature": 0.3})[0] == 200
    assert wait_for(lambda: seen and not app.s.busy)
    _turn(app, "b")
    assert seen[0].get("temperature") == 0.3 and "temperature" not in seen[-1]
    assert "temperature" not in (app.e.current.get("extra_body") or {})
    assert call(app, "/api/send", {"text": "c", "temperature": 9})[0] == 200          # out of range = model default


# I1 search counts
def test_r418_i1_search_counts_hits(app):
    _turn(app, "zebra one")
    _turn(app, "zebra two")
    code, d = call(app, "/api/search?q=zebra")
    row = d["sessions"][0]
    assert code == 200 and row["hits"] >= 2 and row["capped"] is False and "zebra" in row["snippet"]


# I2 cancel one queued
def test_r418_i2_cancel_one_queued_message(app):
    app.s.busy = True
    for t in ("one", "two", "three"):
        assert app.s.send(t)[0]
    assert call(app, "/api/queue/cancel", {"position": 2, "text": "one"})[0] == 409     # wrong text at that place
    assert call(app, "/api/queue/cancel", {"position": 2, "text": "two"})[0] == 200
    assert [q[2] for q in app.s._queue] == ["one", "three"]
    assert any(e["type"] == "queue_cancel" and e["text"] == "two" for e in app.s.events)


# I3 save reply as note
def test_r418_i3_note_keeps_its_source_chat(app):
    _turn(app)
    sid = app.e.session.id
    code, d = call(app, "/api/notes/create", {"title": "r", "text": "reply", "source_session": sid})
    assert code == 200
    assert call(app, "/api/notes/" + d["id"])[1]["source_session"] == sid
    code, d = call(app, "/api/notes/create", {"title": "r", "text": "x", "source_session": "../nope"})
    assert call(app, "/api/notes/" + d["id"])[1]["source_session"] is None


# I4 retry says what went again
def test_r418_i4_retry_event_counts_pictures_and_notes(app):
    seen = _seen(app)
    assert app.s.send("q", notes=[{"title": "A", "text": "x"}])[0]
    assert wait_for(lambda: seen and not app.s.busy)
    assert app.s.retry()[0]
    ev = [e for e in app.s.events if e["type"] == "retry"][-1]
    assert ev["notes"] == 1 and ev["images"] == 0


# I5 + page wiring (static)
def test_r418_page_wiring():
    js, v2 = (WEB / "app.js").read_text(), (WEB / "index_v2.html").read_text()
    assert "function openPanel(" in js and "Back to chat" in js and "dlg.show()" in js
    assert "body[data-panel] .main>#log" in v2 and 'id="temp-on"' in v2 and 'id="ap-left"' in v2 and 'id="note-src"' in v2
    assert "saveChatCopy" in js and "showChatCopy" in js and "CHAT_COPY_KEY" in js
    assert "/api/queue/cancel" in js and "/api/share" in js and "markMatches" in js and "turnLine" in js
