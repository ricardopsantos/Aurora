"""R421: a chat downloads as one HTML file, brief (prompts + answers) or full (plus thinking and tool calls)."""
import urllib.request

from aurora.providers.base import ToolCall, TurnResult
from tests.test_core import FakeProvider
from tests.test_web import TOKEN, app, call, wait_for  # noqa: F401  (fixture)


class Thinker(FakeProvider):
    on_think = None

    def turn(self, model, messages, system, tools_, on_text, cancel):
        if self.on_think:
            self.on_think("pondering <b>round %d</b>" % (self.calls + 1))
        return super().turn(model, messages, system, tools_, on_text, cancel)


def _get(app, path):
    req = urllib.request.Request(app.base + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read().decode()


def _chat(app, tmp_path):
    f = tmp_path / "out.txt"
    app.state["prov"] = Thinker([
        TurnResult(text="", stop_reason="tool_calls", tool_calls=[ToolCall("1", "read_file", {"path": str(f)})]),
        TurnResult(text="The <answer> is 42", stop_reason="end")])
    f.write_text("tool said <hello>")
    app.s.auto_approve = True
    call(app, "/api/send", {"text": "what is <it>?"})
    assert wait_for(lambda: not app.s.busy and any(ev["type"] == "done" for ev in app.s.events), 10)
    return app.e.session.id


def test_r421_thinking_is_logged_in_order(app, tmp_path):
    sid = _chat(app, tmp_path)
    from aurora.session import Session
    evs = [(r["event"], r.get("text", "")) for r in Session(sid).iter_records()
           if r.get("event") in ("user", "thinking", "tool", "assistant")]
    assert [e for e, _ in evs] == ["user", "thinking", "tool", "thinking", "assistant"]
    assert "round 1" in evs[1][1] and "round 2" in evs[3][1]


def test_r421_brief_and_full_html(app, tmp_path):
    sid = _chat(app, tmp_path)
    st, ct, brief = _get(app, f"/api/sessions/{sid}/export?level=brief")
    assert st == 200 and ct.startswith("text/html")
    assert "what is &lt;it&gt;?" in brief and "The &lt;answer&gt; is 42" in brief
    assert "pondering" not in brief and "read_file" not in brief and "<script" not in brief
    full = _get(app, f"/api/sessions/{sid}/export?level=full")[2]
    assert "pondering &lt;b&gt;round 1&lt;/b&gt;" in full and "read_file" in full and "tool said &lt;hello&gt;" in full
    assert full.index("round 1") < full.index("tool said") < full.index("round 2") < full.index("is 42")


def test_r421_bad_level_and_unknown_session(app):
    assert call(app, "/api/sessions/nope/export?level=brief")[0] == 404
    sid = app.e.session.id
    assert call(app, f"/api/sessions/{sid}/export?level=all")[0] == 400


def test_r421_thinking_log_is_capped():
    from aurora.engine import Engine
    logged = []
    e = Engine.__new__(Engine)
    e.session = type("S", (), {"log": lambda self, ev, **k: logged.append((ev, k))})()
    e._thinking, e._think_len = [], 0
    think = e._thinker(object())
    think("x" * (Engine.THINK_LOG_MAX + 500))
    e._flush_thinking()
    assert logged[0][0] == "thinking" and len(logged[0][1]["text"]) == Engine.THINK_LOG_MAX
    assert logged[0][1]["chars"] == Engine.THINK_LOG_MAX + 500
    e._flush_thinking()
    assert len(logged) == 1


def test_r421_chat_search_skips_thinking(app, tmp_path):
    """Thinking is never shown in the chat, so a search hit there would open a chat with nothing to mark."""
    _chat(app, tmp_path)
    from aurora import service
    assert service.search_sessions("pondering") == []
    assert service.search_sessions("answer")
