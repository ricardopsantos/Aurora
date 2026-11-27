"""R454: web security and start-up — card "Aurora - Improve existing features",
part 3 (#17 share links are snapshots that expire and leave tool output out,
#18 a real Content-Security-Policy, #19 wrong-token lockout, #20 starting
without a `local` model)."""
import base64
import hashlib
import json
import re
import time
import urllib.request

import pytest

from aurora.web import server as web_server
from aurora.web import shares
from tests.test_web import TOKEN, app, call  # noqa: F401  (fixture)
from tests.test_web_r418 import _get, _turn


@pytest.fixture(autouse=True)
def _fresh_auth_counts():
    web_server._auth_fails.clear()
    yield
    web_server._auth_fails.clear()


# ── #17: share links ────────────────────────────────────────────────────────
def _share(app):
    code, d = call(app, "/api/share", {"id": app.e.session.id})
    assert code == 200
    return d


def test_a_share_link_shows_the_chat_as_it_was_when_shared(app):  # noqa: F811
    _turn(app, "before sharing")
    d = _share(app)
    _turn(app, "said after sharing")
    code, _, body = _get(app, d["path"])
    assert code == 200
    assert "before sharing" in body and "said after sharing" not in body


def test_sharing_again_moves_the_snapshot_to_now(app):  # noqa: F811
    _turn(app, "first")
    d = _share(app)
    _turn(app, "second")
    assert _share(app)["token"] == d["token"]          # still one link per chat
    assert "second" in _get(app, d["path"])[2]


def test_a_share_link_leaves_tool_output_out(app):  # noqa: F811
    _turn(app, "look at the file")
    app.e.session.log("tool", name="read_file", args={"path": "x"}, output="TOOL-OUTPUT-SECRET")
    app.e.session.log("assistant", text="done reading", model="local")
    d = _share(app)
    body = _get(app, d["path"])[2]
    assert "done reading" in body and "TOOL-OUTPUT-SECRET" not in body


def test_a_share_link_expires(app, monkeypatch):  # noqa: F811
    _turn(app, "hello")
    d = _share(app)
    v = shares.info(d["token"])
    assert v["expires"] > time.time() + 6 * 86400        # the default: 7 days
    monkeypatch.setattr(time, "time", lambda: v["expires"] + 1)
    code, _, body = _get(app, d["path"])
    assert code == 404 and "expired" in body


def test_a_link_made_before_snapshots_is_frozen_at_its_first_read(app):  # noqa: F811
    _turn(app, "old message")
    sid = app.e.session.id
    tok = "A" * 21 + "b"
    shares._save({tok: {"session": sid, "created": 1}})   # the pre-R454 shape
    assert "old message" in _get(app, "/shared/" + tok)[2]
    _turn(app, "newer message")
    assert "newer message" not in _get(app, "/shared/" + tok)[2]
    assert shares.info(tok)["expires"] > time.time()


def test_days_zero_means_the_link_never_expires(app):  # noqa: F811
    _turn(app, "hi")
    tok, _ = shares.create(app.e.session.id, days=0)
    assert shares.info(tok)["expires"] == 0


# ── #18: Content-Security-Policy ────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/", "/paper", "/v1"])
def test_each_page_may_run_only_its_own_scripts(app, path):  # noqa: F811
    code, h, body = _get(app, path)
    assert code == 200
    csp = h["Content-Security-Policy"]
    script_src = re.search(r"script-src ([^;]*)", csp).group(1)
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    for d in ("default-src 'self'", "connect-src 'self'", "object-src 'none'",
              "base-uri 'none'", "frame-ancestors 'none'"):
        assert d in csp
    inline = re.findall(r"<script>(.*?)</script>", body, re.S)
    for js in inline:                                    # every inline script is allowed by its hash
        h256 = base64.b64encode(hashlib.sha256(js.encode()).digest()).decode()
        assert f"'sha256-{h256}'" in script_src
    assert script_src.count("'sha256-") == len(inline)


def test_page_csp_hashes_inline_scripts_only():
    csp = web_server.page_csp(b'<script src="/app.js"></script><script>var a=1;</script>')
    want = base64.b64encode(hashlib.sha256(b"var a=1;").digest()).decode()
    assert csp.endswith(f"script-src 'self' 'sha256-{want}'")


# ── #19: wrong-token lockout ────────────────────────────────────────────────
def _status(app, token, xff=None):  # noqa: F811
    req = urllib.request.Request(app.base + "/api/state")
    req.add_header("Authorization", f"Bearer {token}")
    if xff:
        req.add_header("X-Forwarded-For", xff)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_too_many_wrong_tokens_lock_the_client_out(app):  # noqa: F811
    for _ in range(web_server.AUTH_MAX_FAILS):
        assert _status(app, "wrong") == 401
    assert _status(app, "wrong") == 429
    assert _status(app, TOKEN) == 429          # not even checked while locked
    assert _status(app, TOKEN, xff="203.0.113.9") == 200   # another client behind the proxy


def test_a_right_token_resets_the_count(app):  # noqa: F811
    for _ in range(web_server.AUTH_MAX_FAILS - 1):
        _status(app, "wrong")
    assert _status(app, TOKEN) == 200
    for _ in range(web_server.AUTH_MAX_FAILS - 1):
        assert _status(app, "wrong") == 401


def test_the_lockout_ends(monkeypatch):
    now = 1000.0
    for _ in range(web_server.AUTH_MAX_FAILS):
        web_server.auth_result("c", False, now=now)
    assert web_server.auth_locked("c", now=now + 1) > 0
    assert web_server.auth_locked("c", now=now + web_server.AUTH_LOCK_S + 1) == 0


def test_the_client_behind_a_proxy_is_the_last_forwarded_address():
    assert web_server._client_key("127.0.0.1", "1.1.1.1, 203.0.113.9") == "203.0.113.9"
    assert web_server._client_key("127.0.0.1", None) == "127.0.0.1"
    # a remote peer cannot pick its own key with the header
    assert web_server._client_key("198.51.100.7", "203.0.113.9") == "198.51.100.7"


# ── #20: starting without a `local` model ───────────────────────────────────
def _engine(tmp_path, monkeypatch, models):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n"
                   "  openrouter: {type: openai, base_url: 'https://example.com/v1', api_key: k}\n"
                   "  local: {type: openai, base_url: 'http://x'}\n"
                   "models:\n" + "".join(f"  - {{model: {m}, provider: {p}}}\n" for p, m in models))
    from aurora.engine import Engine
    return Engine(str(cfg))


def test_the_web_starts_on_the_first_usable_model_without_a_local_entry(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch, [("openrouter", "a/b"), ("openrouter", "c/d")])
    assert web_server.start_model(e)["model"] == "a/b"


def test_the_local_entry_is_still_the_default(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch, [("openrouter", "a/b"), ("local", "local")])
    assert web_server.start_model(e)["model"] == "local"


def test_the_pages_remembered_model_wins(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch, [("local", "local"), ("openrouter", "c/d")])
    from aurora import service
    service.save_state(web_last_model="c/d", web_last_provider="openrouter")
    assert web_server.start_model(e)["model"] == "c/d"
