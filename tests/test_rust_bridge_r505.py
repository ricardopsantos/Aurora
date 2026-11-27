"""R504, R505, R506 — the same engine, the same scripted provider object, each core:
the Rust loop (over the provider bridge) must leave the same history and records,
send the model the same system prompt, and word a failed request the same way."""

import json

import pytest

from aurora.providers.base import ProviderError, ToolCall, TurnResult
from tests.test_core import _mk_engine
from tests.test_rust_compat_r482 import aurora_rs  # noqa: F401


class _Script:
    api_key, extra_body, on_think, cache_prompt = "k", {}, None, False

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def turn(self, model, messages, system, tools, on_text, cancel):
        self.seen.append({"system": system, "roles": [m.get("role") for m in messages],
                          "tools": sorted(t["name"] for t in tools or [])})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        on_text(step.text)
        return step

    def assistant_message(self, r):
        m = {"role": "assistant", "content": r.text or None}
        if r.tool_calls:
            m["tool_calls"] = [{"id": c.id, "type": "function",
                                "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in r.tool_calls]
        return m

    def tool_result_message(self, c, o):
        return {"role": "tool", "tool_call_id": c.id, "content": o}

    def cost(self, m, i, o, cached=0):
        return 0.0


class _FE:
    def __init__(self):
        self.notes, self.text = [], []

    on_tool_start = staticmethod(lambda n, a: None)
    on_tool_result = staticmethod(lambda n, o: None)
    approve = staticmethod(lambda *a: "y")
    ask_continue = staticmethod(lambda i: True)
    cancelled = staticmethod(lambda: False)

    def on_text(self, t):
        self.text.append(t)

    def notify(self, m):
        self.notes.append(m)


def _turn(tmp_path, monkeypatch, aurora_rs, core, steps, tools=True):
    monkeypatch.setenv("AURORA_CORE", core)
    monkeypatch.setenv("AURORA_RS", aurora_rs)
    (tmp_path / core).mkdir(exist_ok=True)
    e = _mk_engine(tmp_path / core, monkeypatch)
    e.redact_secrets = False
    prov = _Script(steps)
    e.current["tools"] = tools
    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: prov)
    fe = _FE()
    try:
        e.send("please write the note", fe)
        assert (core == "rust") == (getattr(e, "_rust_core", None) is not None)
    finally:
        if getattr(e, "_rust_core", None):
            assert core == "rust"
            e._rust_core.close()
    recs = [json.loads(x) for f in sorted((tmp_path / core / "home" / "sessions").glob("*.jsonl"))
            for x in f.read_text().splitlines()]
    return e, prov, fe, recs


def _shape(recs):
    keep = ("event", "name", "args", "output", "text", "decision")
    return [{k: r[k] for k in keep if k in r} for r in recs if r.get("event") in ("user", "tool", "assistant", "approval")]


def test_same_history_records_and_system_prompt_on_both_cores(tmp_path, monkeypatch, aurora_rs):
    got = {}
    for core in ("python", "rust"):
        note = tmp_path / core / "note.txt"
        steps = [TurnResult(text="", tool_calls=[ToolCall("c1", "write_file", {"path": str(note), "content": "hi\n"})],
                            stop_reason="tool_use", input_tokens=10, output_tokens=2),
                 TurnResult(text="Wrote it.", stop_reason="end", input_tokens=20, output_tokens=3)]
        e, prov, fe, recs = _turn(tmp_path, monkeypatch, aurora_rs, core, steps)
        assert note.read_text() == "hi\n"
        msgs = [{k: v for k, v in m.items() if k != "content" or m["role"] != "tool"} for m in e.messages]
        rows = json.loads(json.dumps(_shape(recs)).replace(str(tmp_path / core), "<T>"))
        got[core] = (msgs and [m["role"] for m in msgs], rows, prov.seen, "".join(fe.text))
    assert got["python"][0] == got["rust"][0] == ["user", "assistant", "tool", "assistant"]
    assert got["python"][1] == got["rust"][1]
    py_seen, rs_seen = got["python"][2], got["rust"][2]
    assert [s["roles"] for s in py_seen] == [s["roles"] for s in rs_seen]
    assert py_seen[0]["system"] == rs_seen[0]["system"]          # R506
    assert py_seen[0]["tools"] == rs_seen[0]["tools"]
    assert "Wrote it." in got["rust"][3]


@pytest.mark.parametrize("err", ["local request failed (ConnectError): tcp connect error: Connection refused (os error 111)",
                                 "remote rate-limited (429): slow down",
                                 "remote request failed: the request exceeds the available context size"])
def test_a_failed_request_is_worded_the_same(tmp_path, monkeypatch, aurora_rs, err):
    notes = {}
    for core in ("python", "rust"):
        _e, _p, fe, _r = _turn(tmp_path, monkeypatch, aurora_rs, core, [ProviderError(err)])
        notes[core] = [n for n in fe.notes if "Rust core" not in n]
    assert notes["python"] == notes["rust"], notes
    assert not any("request failed" in n and "ConnectError" in n for n in notes["rust"])     # R504: no raw error


def test_a_model_without_tools_gets_none_on_both_cores(tmp_path, monkeypatch, aurora_rs):
    seen = {}
    for core in ("python", "rust"):
        _e, prov, _fe, _r = _turn(tmp_path, monkeypatch, aurora_rs, core, [TurnResult(text="hi", stop_reason="end")], tools=False)
        seen[core] = prov.seen[0]["tools"]
    assert seen == {"python": [], "rust": []}
