"""R345 — a failed backend is explained: cause + fixes, at startup, in /status
and after a failed turn."""

import pytest

from aurora import agent, diagnose, ui
from aurora.providers.base import ProviderError
from tests.test_core import _cb, _mk_engine


@pytest.mark.parametrize("err,kind", [
    ("[Errno 111] Connection refused", "refused"),
    ("timed out", "timeout"),
    ("[Errno -2] Name or service not known", "dns"),
    ("Client error '401 Unauthorized' for url", "auth"),
    ("403 Forbidden", "auth"),
    ("404 Not Found", "notfound"),
    ("certificate verify failed", "tls"),
    ("429 Too Many Requests", "ratelimit"),
    ("something odd", "other"),
])
def test_classify(err, kind):
    assert diagnose.classify(err) == kind


@pytest.mark.parametrize("kind", diagnose.KINDS)
def test_every_kind_has_a_cause_and_at_least_two_fixes(kind):
    d = diagnose.diagnose(kind, provider="p", url="http://h:1/v1", model="m",
                          key_env="MY_KEY", detail="boom")
    assert d.cause and len(d.fixes) >= 2
    text = " ".join([d.cause, *d.fixes]).lower()
    for personal in ("tailscale", "m7", "llamadesk", "ricardo", "hermes"):
        assert personal not in text


def test_auth_and_nokey_name_the_key_variable():
    assert "MY_KEY" in " ".join(diagnose.diagnose("auth", key_env="MY_KEY").fixes)
    assert "aurora key set MY_KEY" in " ".join(
        diagnose.diagnose("nokey", provider="p", key_env="MY_KEY").fixes)


def test_ollama_notfound_says_pull_the_model():
    d = diagnose.diagnose("notfound", model="qwen3:1.7b", is_ollama=True)
    assert "ollama pull qwen3:1.7b" in " ".join(d.fixes)


def test_render_shape():
    lines = diagnose.render(diagnose.diagnose("refused", url="http://h:1/v1"))
    assert lines[0].strip().startswith("cause:")
    assert lines[1].strip().startswith("fix:")
    assert all("•" in l for l in lines[1:])


def _failed_engine(tmp_path, monkeypatch, health):
    e = _mk_engine(tmp_path, monkeypatch)
    e.current = {"model": "local", "provider": "local"}
    monkeypatch.setattr(e, "_provider_health_uncached", lambda: health)
    return e


def test_engine_diagnosis_for_a_refused_local_server(tmp_path, monkeypatch):
    h = {"ok": False, "detail": "unreachable: connection refused", "kind": "refused",
         "url": "http://127.0.0.1:8080/v1"}
    e = _failed_engine(tmp_path, monkeypatch, h)
    d = e.diagnosis_for(h)
    assert d.kind == "refused" and "127.0.0.1:8080" in d.cause
    assert e.diagnosis_for({"ok": True, "detail": "ready"}) is None


def test_diagnosis_lines_are_empty_when_healthy_and_shown_when_not(tmp_path, monkeypatch):
    e = _failed_engine(tmp_path, monkeypatch, {})
    assert ui.diagnosis_lines(e, {"ok": True, "detail": "ready"}) == []
    lines = ui.diagnosis_lines(e, {"ok": False, "detail": "x", "kind": "timeout"})
    assert any("cause:" in l for l in lines) and any("fix:" in l for l in lines)


def test_startup_offers_the_picker_only_when_unhealthy(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(ui, "_pick_model", lambda eng, fe, **k: opened.append(1))
    ui._offer_model_picker(e, None, {"ok": True, "detail": "ready"})
    assert opened == []
    ui._offer_model_picker(e, None, {"ok": False, "detail": "down", "kind": "refused"})
    assert opened == [1]


def test_a_broken_picker_never_stops_startup(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(ui, "_pick_model",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    ui._offer_model_picker(e, None, {"ok": False, "detail": "down"})   # no raise


def test_remote_model_without_a_key_reports_nokey(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    e.current = {"model": "some/remote", "provider": "local"}
    prov = e._provider_for(e.current)
    monkeypatch.setattr(prov, "api_key", "", raising=False)
    monkeypatch.setattr(prov, "pick_endpoint", lambda cache_ok=True: prov.base_url)
    h = e._provider_health_uncached()
    assert h["ok"] is False and h["kind"] == "nokey"


class _Rejecting:
    name, base_url = "private-key-name", "https://api.example.com/v1"

    def turn(self, *a, **k):
        raise ProviderError("private-key-name request failed: 401 Unauthorized")


def test_a_rejected_key_mid_session_gets_cause_and_fixes():
    log = []
    agent.run_turn(_Rejecting(), "m", [{"role": "user", "content": "hi"}], "s",
                   _cb(log=log), 5, True)
    note = next(e[1] for e in log if e[0] == "notify")
    assert "rejected the API key" in note and "aurora key set" in note
    assert "private-key-name" not in note


class _Down:
    name, base_url = "x", "https://someone-box.example.ts.net:1/v1"

    def turn(self, *a, **k):
        raise ProviderError("x request failed: [Errno 111] Connection refused")


def test_unreachable_mid_session_lists_fixes_without_leaking_the_host():
    log = []
    agent.run_turn(_Down(), "m", [{"role": "user", "content": "hi"}], "s",
                   _cb(log=log), 5, True)
    note = next(e[1] for e in log if e[0] == "notify")
    assert "local backend unreachable" in note and "fix:" in note
    assert "someone-box" not in note.lower()
