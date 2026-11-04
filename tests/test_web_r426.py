"""R426: a sleeping local model wakes on request: one-token completion, then the loaded name is known at once."""
from aurora.providers.base import TurnResult
from tests.test_core import FakeProvider
from tests.test_web import app, call, wait_for  # noqa: F401  (fixture)


class Sleepy(FakeProvider):
    extra_body = None
    on_think = None
    awake = False
    seen = None

    def turn(self, model, messages, system, tools_, on_text, cancel):
        Sleepy.seen = dict(self.extra_body or {})
        self.awake = True
        return super().turn(model, messages, system, tools_, on_text, cancel)

    def live_model_name(self):
        return "models/qwen3-9b.gguf" if self.awake else None


def test_r426_wake_sends_one_token_and_reports_the_model(app):
    p = Sleepy([TurnResult(text="h", stop_reason="end")])
    app.state["prov"] = p
    st, out = call(app, "/api/wake", {})
    assert st == 200 and out.get("waking")
    assert wait_for(lambda: any(ev["type"] == "notify" and "awake: qwen3-9b.gguf" in ev.get("text", "") for ev in app.s.events), 5)
    assert Sleepy.seen.get("max_tokens") == 1 and p.extra_body is None          # the one-token cap is not left behind
    assert call(app, "/api/state")[1]["loaded"] is True
    assert call(app, "/api/wake", {})[1].get("awake") is True                  # awake: nothing sent again
    assert p.calls == 1


def test_r426_remote_model_needs_no_wake(app):
    app.e.current = next(m for m in app.e.models if m["model"] == "other")
    assert call(app, "/api/wake", {})[1].get("awake") is True
