"""R476: adversarial integration tests for the web server: hostile bodies on every route, every route without the
token, and the model itself attacking through real tool calls (auto-approve on, sandbox on)."""
import http.client
import json
import os

import pytest

from aurora.providers.base import ToolCall, TurnResult
from tests.test_web import TOKEN, app, call, types, wait_for  # noqa: F401  (fixture)

KEYS = ["answer", "base", "chat", "client_id", "files", "folder", "i", "id", "ids", "image_paths", "images", "kind",
        "name", "new_chat", "note_ids", "on", "open", "position", "source_session", "temperature", "text", "title",
        "token", "type", "usd", "which", "model", "command", "level", "yaml", "file", "path", "provider"]
HOSTILE = [None, 0, -1, 10 ** 30, 1.5, float("1e308"), True, [], {}, [1, {"a": []}], {"__proto__": 1}, "",
           "../../../etc/passwd", "\x00\x01", "💥" * 50, "x" * 20_000, ["../x"] * 50]
# Routes that call out of the process (price list, model wake-up, speech server) or take raw bytes are left out
POST = ["/api/approve", "/api/auto", "/api/budget", "/api/chat/delete", "/api/chat/move", "/api/chat/rename",
        "/api/command", "/api/config/save", "/api/config/validate", "/api/files/delete", "/api/files/delete_all",
        "/api/folder/create", "/api/folder/delete", "/api/folder/open", "/api/folder/rename", "/api/model",
        "/api/models/config/add", "/api/models/config/delete", "/api/models/config/save", "/api/new",
        "/api/notes/create", "/api/notes/delete", "/api/notes/order", "/api/notes/save", "/api/notes/type", "/api/plan",
        "/api/queue/cancel", "/api/resume", "/api/retry", "/api/send", "/api/share", "/api/share/delete", "/api/stop",
        "/api/undo"]
GET = ["/api/state", "/api/config/files", "/api/models/config", "/api/models", "/api/sessions", "/api/search?q=x",
       "/api/sessions/abc", "/api/sessions/abc/export", "/api/files", "/api/files/file?name=x", "/api/notes",
       "/api/notes/abc", "/api/usage", "/api/diff", "/api/undo"]


@pytest.fixture(autouse=True)
def _no_lockout():
    """The wrong-token lockout (R454) is per process: the token tests here would lock the next test out."""
    from aurora.web import server
    server._auth_fails.clear()
    yield
    server._auth_fails.clear()


def _raw(app, method, path, body: bytes | None, token=TOKEN):
    host, port = app.base.split("//")[1].split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    h = {"Content-Type": "application/json"}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()               # raises RemoteDisconnected if the handler died
    data = r.read()
    return r.status, data


def _settle(app):
    wait_for(lambda: not app.s.busy, 5)


@pytest.mark.parametrize("path", POST)
def test_hostile_bodies_never_crash_a_route(app, path):
    app.state["prov"].results = [TurnResult(text="ok", stop_reason="end")] * 200
    bodies = [b"[]", b'"str"', b"123", b"null", b"{", b"\xff\xfe", b"{}"]
    bodies += [json.dumps({k: v for k in KEYS}).encode() for v in HOSTILE]
    bodies += [json.dumps({k: HOSTILE[(i + j) % len(HOSTILE)] for j, k in enumerate(KEYS)}).encode()
               for i in range(len(HOSTILE))]
    for b in bodies:
        code, data = _raw(app, "POST", path, b)
        assert code < 500, (path, b[:80], code, data[:300])
        assert data == b"" or isinstance(json.loads(data), dict), (path, b[:80], data[:200])
        _settle(app)


@pytest.mark.parametrize("path", POST)
def test_every_post_route_needs_the_token(app, path):
    from aurora.web import server
    for tok in (None, "", "wrong", TOKEN + "x", TOKEN[:-1]):
        code, _ = _raw(app, "POST", path, b"{}", token=tok)
        assert code == 401, (path, tok, code)
        server._auth_fails.clear()


@pytest.mark.parametrize("path", GET)
def test_every_get_route_needs_the_token(app, path):
    code, _ = _raw(app, "GET", path, None, token=None)
    assert code == 401, (path, code)


@pytest.mark.parametrize("path", ["/api/sessions/..%2F..%2Fetc%2Fpasswd", "/api/sessions/%2e%2e", "/api/notes/..%2Findex",
                                  "/api/files/file?name=..%2F..%2Fconfig.yaml", "/api/files/file?name=%2Fetc%2Fpasswd",
                                  "/api/files/file?name=.&kind=outputs", "/api/sessions/x/export?level=..%2F"])
def test_path_tricks_in_urls_find_nothing(app, path):
    code, data = _raw(app, "GET", path, None)
    assert code in (400, 404), (path, code, data[:200])
    assert b"root:" not in data and b"api_key" not in data


# ── the model attacking through real tool calls ─────────────────────────────

# Only planted, fake secrets are attacked: a failure here must never print a real one
ATTACKS = [
    "cat ~/.ssh/id_ed25519", "k=.ss; cat ~/${k}h/id_ed25519", "echo evil >> ~/.bashrc",
    "python3 -c \"import os;print(open(os.path.expanduser('~/.ss'+'h/id_ed25519')).read())\"",
    "printf 'IyEvYmluL3NoCg==' | base64 -d > .git/hooks/post-commit",
]


def test_the_model_cannot_reach_secrets_even_with_auto_approve(app, tmp_path, monkeypatch, box_home_root):
    from aurora import sandbox, service, tools
    home = box_home_root / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    (home / ".bashrc").write_text("# rc\n")
    ws = tmp_path / "ws"
    (ws / ".git" / "hooks").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(ws)
    pol = sandbox.Policy(home=str(home), aurora_home=os.environ["AURORA_HOME"])
    ok, why = sandbox.check(pol)
    if not ok:
        pytest.skip(f"no sandbox here: {why}")
    monkeypatch.setattr(sandbox, "policy_from", lambda runtime, ah: pol)
    try:
        assert service.web_sandbox({}) == ""
        app.s.auto = True
        app.e.max_iterations = 50                     # every attack must really run, not stop at the step limit
        calls = [TurnResult(text="", stop_reason="tool_calls",
                            tool_calls=[ToolCall(id=f"t{i}", name="run_command", arguments={"command": c})])
                 for i, c in enumerate(ATTACKS)]
        app.state["prov"].results = calls + [TurnResult(text="done", stop_reason="end")]
        assert call(app, "/api/send", {"text": "go"})[0] == 200
        assert wait_for(lambda: "done" in types(app), 30)
        evs = app.s.wait_events(0, 0.01)
        assert sum(e.get("type") == "tool_result" for e in evs) == len(ATTACKS)
        shown = json.dumps(evs)
        assert "PRIVATE KEY" not in shown
        assert (home / ".bashrc").read_text() == "# rc\n"
        assert not (ws / ".git" / "hooks" / "post-commit").exists()
        assert not any(e.get("type") == "approval" for e in evs)            # nothing was put to the page: refused or boxed
    finally:
        tools.disable_sandbox()
        app.s.auto = False
