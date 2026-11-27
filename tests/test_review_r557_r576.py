"""R557-R576: 20 bugs and slow paths in the Rust core and the web page's server (owner, 2026-11-27).
The Rust ones are tested in their crates (cargo test); this file covers the Python side and the shared rules."""
import json
import urllib.error
import urllib.request

from aurora import tools
from aurora.web import core as web_core
from aurora.web import notes
from aurora.web import server as web_server
from tests.test_web import TOKEN, app, call  # noqa: F401  (fixture)


def _raw_post(app, path, data: bytes):  # noqa: F811
    req = urllib.request.Request(app.base + path, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as ex:
        return ex.code, json.loads(ex.read() or b"{}")


# ── R565: a trailing dot does not hide a private name ──
def test_r565_fully_qualified_private_names():
    for h in ("localhost.", "nas.local.", "box.example.ts.net.", "127.0.0.1.", "[::1]"):
        assert tools._private_host(h, resolve=False), h
    assert not tools._private_host("example.com.", resolve=False)


# ── R566: the squeezed output keeps exactly `limit` characters ──
def test_r566_squeeze_halves_add_up():
    out = tools._squeeze("x" * 100, 21)
    assert out.startswith("x" * 11 + "\n[... 79 chars omitted")
    assert out.endswith("\n" + "x" * 10)
    assert tools._squeeze("abcdef", 1) == "a\n[... 5 chars omitted from the middle ...]\n"   # was the whole text
    assert tools._squeeze("abc", 3) == "abc"
    assert tools._squeeze("abcd", 2) == "a\n[... 2 chars omitted from the middle ...]\nd"


# ── R572: app.js's version follows its bytes, never an object id ──
def test_r572_script_version_follows_the_bytes(monkeypatch, tmp_path):
    js = tmp_path / "app.js"
    js.write_text("one")
    monkeypatch.setattr(web_server, "SCRIPT", js)
    monkeypatch.setattr(web_server, "_version", None)
    a = web_server._script_version()
    assert web_server._script_version() == a
    js.write_text("two!")
    b = web_server._script_version()
    assert b != a and len(b) == 12
    js.write_text("one")
    import os
    os.utime(js, (1, 1))                                   # same bytes again under a new mtime: same hash
    assert web_server._script_version() == a


# ── R573: the replay buffer trims in batches, and stays within its caps ──
def test_r573_event_buffer_trims_in_batches(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(web_core, "MAX_EVENTS", 80)
    s = app.s
    for i in range(200):
        s.emit("text", text=str(i))
    n = len(s.events)
    assert 70 <= n <= 80, n
    ids = [e["id"] for e in s.events]
    assert ids == list(range(ids[0], ids[0] + n))         # still contiguous: wait_events finds the start by arithmetic
    assert s._bytes == sum(s._sizes) and len(s._sizes) == n
    got = s.wait_events(ids[-3], 0)
    assert [e["text"] for e in got] == ["198", "199"]


def test_r573_byte_cap_still_holds(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(web_core, "MAX_EVENT_BYTES", 10_000)
    s = app.s
    for _ in range(50):
        s.emit("text", text="y" * 1000)
    assert s._bytes <= 10_000 and len(s.events) >= 1


# ── R574: a hand-edited notes index never breaks the list or a save ──
def test_r574_hand_edited_index(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    nid, _ = notes.create("t", "body")
    other, _ = notes.create("u", "more")
    idx = json.loads((tmp_path / "web-notes" / "index.json").read_text())
    idx[nid]["updated"] = "yesterday"
    idx[nid]["title"] = 42
    idx[other]["pos"] = True
    del idx[other]["preview"]
    (tmp_path / "web-notes" / "index.json").write_text(json.dumps(idx))
    rows = notes.listing()
    assert {r["id"] for r in rows} == {nid, other}
    assert notes.listing("42")[0]["id"] == nid
    upd, why = notes.save(nid, "t2", "new body", None)
    assert upd and not why
    assert notes.get(nid)["text"] == "new body"


# ── R576: a body that is not JSON is a 400, not an action run with defaults ──
def test_r576_bad_json_is_refused(app):  # noqa: F811
    before = app.e.session.id
    code, d = _raw_post(app, "/api/new", b"{not json")
    assert code == 400 and "not a JSON object" in d["error"]
    assert app.e.session.id == before                   # no new chat was started
    assert _raw_post(app, "/api/auto", b"[true]")[0] == 400
    assert _raw_post(app, "/api/files/delete", b"nope")[0] == 400
    assert call(app, "/api/new", {})[0] == 200           # a real (empty) object still works
    req = urllib.request.Request(app.base + "/api/stop", data=b"", method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=5) as r:     # no body at all is still {}
        assert r.status == 200


# ── R558: IPv4-mapped IPv6 and a trailing dot classify like the host they name ──
def test_r558_lan_host_mapped_and_dotted():
    from aurora.providers import openai_compat as oc
    assert oc._is_lan_host("http://[::ffff:192.168.1.5]:8080/v1")
    assert oc._is_lan_host("http://localhost.:8080/v1")
    assert not oc._is_lan_host("http://[::ffff:8.8.8.8]/v1")
    assert oc._is_bare_ip("https://[::ffff:10.0.0.2]/v1")
    assert not oc._is_bare_ip("https://[::ffff:8.8.8.8]/v1")


# ── R560: tool-call deltas without an index ──
def test_r560_calls_without_index_split_by_id():
    from aurora.providers.openai_compat import _call_index
    pending = {}
    for tc in ({"id": "a"}, {}, {"id": "a"}, {"id": "b"}, {}, {"index": 7}):
        i = _call_index(tc, pending)
        slot = pending.setdefault(i, {"id": "", "name": "", "args": []})
        if tc.get("id"):
            slot["id"] = tc["id"]
    assert sorted(pending) == [0, 1, 7] and pending[0]["id"] == "a" and pending[1]["id"] == "b"


# ── R562: a cancelled fold folds nothing ──
def test_r562_cancelled_compaction_keeps_history(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  local: {type: openai, base_url: 'http://x'}\nmodels:\n  - {model: local, provider: local}\n")
    from aurora.engine import Engine
    from aurora.providers.base import TurnResult
    from tests.test_core import FakeProvider
    e = Engine(str(cfg))
    e.current = e.models[0]
    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: FakeProvider([TurnResult(text="sum", stop_reason="cancelled")]))
    e.messages = [{"role": "user", "content": "a" * 400}, {"role": "assistant", "content": "b" * 400}]
    before = list(e.messages)
    assert e.compact_history(cancel=lambda: True) == 0
    assert e.messages == before
    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: FakeProvider([TurnResult(text="sum", stop_reason="end")]))
    assert e.compact_history(cancel=lambda: False) == 2


# ── R563: the tokens/s clock starts at the first thought ──
def test_r563_gen_clock_starts_at_reasoning(monkeypatch):
    from aurora import agent
    from aurora.providers.base import TurnResult
    from tests.test_core import _cb
    monkeypatch.setenv("AURORA_CORE", "python")              # the Python loop (the Rust one is tested in its crate)
    now = [0.0]
    monkeypatch.setattr(agent, "_gen_clock", lambda: now[0])

    class Thinker:
        on_think = None

        def turn(self, model, messages, system, tools_, on_text, cancel):
            now[0] = 1.0
            if self.on_think:
                self.on_think("hmm")
            now[0] = 5.0
            on_text("ok")
            now[0] = 9.0
            return TurnResult(text="ok", stop_reason="end", output_tokens=40)

        def assistant_message(self, r):
            return {"role": "assistant", "content": r.text}

        def tool_result_message(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": output}

    p = Thinker()
    t = agent.run_turn(p, "m", [], "", _cb(), 5, True)
    assert t.gen_tokens == 40 and t.gen_secs == 8.0       # 9 - 1, not 9 - 5
    assert p.on_think is None                             # restored after the request


# ── R564: 429 as a whole number ──
def test_r564_rate_limit_hint_needs_a_whole_429():
    from aurora import agent
    hit = agent.provider_error_notices(None, "local HTTP 429: slow down")
    miss = agent.provider_error_notices(None, "connect to 10.0.0.1:4291 refused")
    assert any("rate-limited" in n for n in hit)
    assert not any("rate-limited" in n for n in miss)


# ── R567: a background job is done when its shell exits ──
def test_r567_job_done_with_a_lingering_child():
    import time as _t
    jid = tools._start_background("sleep 20 & echo started", None)
    try:
        end = _t.time() + 5
        while _t.time() < end and tools.check_command(jid).startswith("[still running]"):
            _t.sleep(0.05)
        r = tools.check_command(jid)
        assert r.startswith("[exit 0]") and "started" in r, r
        assert tools.cancel_command(jid) == f"[{jid}] kill signal sent"
        end = _t.time() + 5
        while _t.time() < end and tools._BG_JOBS[jid].lingering:
            _t.sleep(0.05)
        assert not tools._BG_JOBS[jid].lingering
    finally:
        tools._kill_group(tools._BG_JOBS[jid].proc, tools._BG_JOBS[jid].pgid)


# ── R577: note creation times never tie ──
def test_r577_created_is_strictly_increasing(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(notes, "_now", lambda: 1000.0)       # every note made "in the same millisecond"
    ids = [notes.create(f"n{i}", "x")[0] for i in range(5)]
    made = [notes.get(i) and notes.listing() for i in ids][-1]
    created = {r["id"]: r["created"] for r in made}
    assert [created[i] for i in ids] == sorted({created[i] for i in ids})


# ── R578: the terminal never starts on a model nobody picked ──
def test_r578_no_model_selected_until_picked(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  ollama: {type: ollama, base_url: 'http://127.0.0.1:9/v1'}\n"
                   "models:\n  - {model: tiny, provider: ollama}\n  - {model: big, provider: ollama}\n")
    from aurora.engine import Engine
    e = Engine(str(cfg))
    assert e.model_restored is False and e.current["model"] == "tiny"     # the default still exists for -t / --web
    e.current = {}                                                        # what the terminal starts with
    assert e.provider_health() == {"ok": False, "detail": "no model selected — /model to pick one"}
    said = []
    e.send("hi", type("F", (), {"notify": lambda s, m: said.append(m)})())
    assert said and said[-1].startswith("no model selected")
    assert not e.messages                                                 # nothing was sent
    e.switch_model(e.models[1])
    e2 = Engine(str(cfg))
    assert e2.model_restored is True and e2.current["model"] == "big"


def test_r578_main_clears_an_unpicked_default():
    import inspect
    from aurora import __main__ as m
    src = inspect.getsource(m)
    assert "if not engine.model_restored and not engine.messages and len(engine.models) > 1" in src
    assert src.index("engine.current = {}") > src.index("serve(engine")     # after --web and -t, only the terminal


# ── R579: advice that fits the machine ──
def test_r579_no_ollama_serve_where_ollama_is_missing(monkeypatch):
    from aurora import diagnose
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert diagnose.server_installed(True, "http://localhost:11434/v1") is False
    assert diagnose.server_installed(True, "http://10.0.0.9:11434/v1") is None      # another machine's Ollama
    assert diagnose.server_installed(False, "http://localhost:8080/v1") is None     # llama.cpp: no fixed command
    d = diagnose.diagnose("refused", provider="ollama", url="http://localhost:11434/v1", model="qwen3:1.7b",
                          is_ollama=True, installed=False, alternatives=["gpu-box · local"])
    text = "\n".join(diagnose.render(d))
    assert "ollama serve" not in text and "not installed on this machine" in text
    assert d.fixes[0] == "/model — switch to `gpu-box · local` (answer now)"
    d = diagnose.diagnose("refused", is_ollama=True, installed=True)
    assert d.fixes[0] == "start Ollama (`ollama serve`)"                              # unchanged when it is there


def test_r579_answering_models_lists_live_servers_first(tmp_path, monkeypatch):
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n"
                   "  dead: {type: ollama, base_url: 'http://127.0.0.1:9/v1'}\n"
                   f"  live: {{type: openai, base_url: 'http://127.0.0.1:{port}/v1', api_key: none}}\n"
                   "models:\n  - {model: a, provider: dead}\n  - {model: local, provider: live}\n")
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        assert e.answering_models(exclude=e.models[0]) == ["live · local"]
        assert e.answering_models(exclude=e.models[1]) == []
    finally:
        srv.close()


# ── R580: configured models that cannot work here are named at start and cannot be picked ──
def _r580_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("aurora.diagnose.server_installed", lambda is_ollama, url="": False if is_ollama else None)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  ol: {type: ollama, base_url: 'http://127.0.0.1:9/v1'}\n"
                   "models:\n  - {model: 'qwen:1b', provider: ol}\n")
    from aurora.engine import Engine
    return Engine(str(cfg))


def test_r580_startup_names_models_that_cannot_work(tmp_path, monkeypatch, capsys):
    from aurora import ui
    e = _r580_engine(tmp_path, monkeypatch)
    bad = ui.model_sanity(e)
    assert [(m["model"], why) for m, why in bad] == [("qwen:1b", "Ollama is not installed on this machine")]
    ui._report_model_sanity(e)
    out = capsys.readouterr().out
    assert "1 configured model won't work here" in out and "ol · qwen:1b" in out
    assert e.unusable_models == {("ol", "qwen:1b"): "Ollama is not installed on this machine"}


def test_r580_an_unusable_model_is_not_picked(tmp_path, monkeypatch, capsys):
    from aurora import ui
    e = _r580_engine(tmp_path, monkeypatch)
    e.current = {}
    e.unusable_models = {("ol", "qwen:1b"): "Ollama is not installed on this machine"}

    class FE:
        def select(self, prompt, options, **k):
            return "1"
        select_menu = select

        def update_menu_labels(self, *a, **k):
            pass
    monkeypatch.setattr(ui, "select", lambda prompt, options, **k: "1")
    ui._pick_model(e, FE())
    assert e.current == {}
    assert "can't be used here: Ollama is not installed on this machine" in capsys.readouterr().out
