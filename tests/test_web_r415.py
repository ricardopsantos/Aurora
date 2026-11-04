"""R415 (web bugs), R416 (web performance), R417 (web QA): card 20261103-104015-auro-web-issues."""
import pathlib
import threading

from aurora.web import core
from aurora.web import server as web_server
from tests.test_web import app, call, types, wait_for  # noqa: F401  (fixture)
from tests.test_web_r401 import _seen

IMG = "data:image/png;base64,AAAA"


def test_r415_bug1_racing_resends_of_one_id_queue_once(app):
    app.s.busy = True
    go = threading.Barrier(8)

    def send():
        go.wait()
        app.s.send("hello", client_id="same")
    ts = [threading.Thread(target=send) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(app.s._queue) == 1


def test_r415_bug5_a_message_queued_during_a_claim_starts_on_release(app):
    assert app.s._claim()
    assert app.s.send("waiting") == (True, "")
    assert app.s._queue
    app.s._release()
    assert wait_for(lambda: "done" in types(app)) and not app.s._queue


def test_r415_bug3_retry_keeps_the_pictures(app):
    app.e.current["vision"] = True
    assert app.s.send("look", images=[IMG])[0]
    assert wait_for(lambda: "done" in types(app) and not app.s.busy)
    got = []
    app.s._turn = lambda text, images=None, temp=None: (got.append(images), app.s._finish())
    assert app.s.retry() == (True, "")
    assert got == [[IMG]]
    assert [e for e in app.s.events if e["type"] == "user"][-1]["images"] == 1


def _note_turn(app):
    seen = _seen(app)
    assert app.s.send("question", notes=[{"title": "Alpha", "text": "ALPHA-BODY"}, {"title": "Beta", "text": "BETA-BODY"}])[0]
    assert wait_for(lambda: len(seen) == 1 and not app.s.busy)
    return seen


def test_r415_b6_edit_keeps_only_the_notes_still_named(app):
    seen = _note_turn(app)
    assert app.s.retry("📝 Beta\nnew question") == (True, "")
    assert wait_for(lambda: len(seen) == 2 and not app.s.busy)
    assert "BETA-BODY" in seen[1] and "ALPHA-BODY" not in seen[1] and seen[1].endswith("new question")


def test_r415_b6_deleting_the_titles_line_detaches_all(app):
    seen = _note_turn(app)
    assert app.s.retry("just this") == (True, "")
    assert wait_for(lambda: len(seen) == 2 and not app.s.busy)
    assert seen[1] == "just this"


def test_r415_b6_regenerate_still_sends_the_notes(app):
    seen = _note_turn(app)
    assert app.s.retry() == (True, "")
    assert wait_for(lambda: len(seen) == 2 and not app.s.busy)
    assert "ALPHA-BODY" in seen[1] and "BETA-BODY" in seen[1]


def test_r415_b10_diff_is_refused_while_a_turn_runs(app):
    app.s.busy = True
    d = app.s.diff()
    assert d["diff"] == "" and "turn is running" in d["note"]


def test_r415_b9_regenerate_after_auto_clear_says_why(app):
    app.s._cleared = True
    app.e.messages[:] = [m for m in app.e.messages if m.get("role") == "system"]
    ok, why = app.s.retry()
    assert not ok and "context filled up" in why


def test_r415_b7_live_chat_read_mid_turn_is_marked(app):
    assert app.s.send("hi there")[0]
    assert wait_for(lambda: "done" in types(app) and not app.s.busy)
    sid = app.e.session.id
    assert "in progress" not in app.s.session_markdown(sid)
    app.s.busy = True
    assert app.s.session_markdown(sid).endswith("the reply being written is not shown yet]")


def test_r415_bug2_notes_conflict_on_page_exit_keeps_a_copy():
    js = (pathlib.Path(web_server.__file__).parent / "app.js").read_text()
    assert "if (e.status === 409 && keep)" in js and "(conflict copy)" in js


def test_r416_event_sizes_are_kept_once_and_trim_stays_under_the_cap(app, monkeypatch):
    monkeypatch.setattr(core, "MAX_EVENT_BYTES", 50_000)
    calls = []
    real = core._ev_size
    monkeypatch.setattr(core, "_ev_size", lambda ev: (calls.append(1), real(ev))[1])
    for i in range(200):
        app.s.emit("tool_start", name="x", args={"a": "y" * 1000})
    assert len(calls) == 200                                # once per event, never again in the trim
    assert app.s._bytes == sum(app.s._sizes) <= 50_000
    assert len(app.s._sizes) == len(app.s.events)


def test_r416_search_cache_drops_half_and_scans_a_capped_tail(app, monkeypatch):
    from aurora import session as S
    assert app.s.send("the zebra question")[0]
    assert wait_for(lambda: "done" in types(app) and not app.s.busy)
    S._SEARCH.clear()
    monkeypatch.setattr(S, "_SEARCH_MAX", 10)
    for i in range(11):
        S._SEARCH[("x", 0.0, str(i))] = ()
    assert S.search_sessions("zebra")
    assert 5 <= len(S._SEARCH) <= 8 and ("x", 0.0, "10") in S._SEARCH
    S._SEARCH.clear()
    monkeypatch.setattr(S, "SEARCH_SCAN_BYTES", 1)
    assert S.search_sessions("zebra") == []


def test_r417_sw_keys_and_folder_open_answer():
    d = pathlib.Path(web_server.__file__).parent
    assert "networkFirst(req, p + url.search)" in (d / "sw.js").read_text()
    assert 'why = "" if ok else "no such folder"' in (d / "server.py").read_text()
