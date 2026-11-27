"""R509-R512: the 2026-11-27 engineering review (F1-F6): code drift on /health, connection and memory caps,
config secrets inside values, web_fetch's private-address check before the request."""
import io
import socket
import threading
import time
import tracemalloc
import urllib.request
from pathlib import Path

import pytest

from aurora.web import attach
from aurora.web import server as web_server
from tests.test_web import TOKEN, app, call  # noqa: F401  (fixture)


def _req(app, path, headers=None, data=None):
    h = {"Authorization": f"Bearer {TOKEN}", **(headers or {})}
    req = urllib.request.Request(app.base + path, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# ── R509 (F1): /health says when the process runs older code than the disk has, and which Rust core it uses ──
def test_r509_health_reports_code_drift(app, monkeypatch, tmp_path):
    monkeypatch.setattr(web_server, "_drift_cache", (0.0, {}))
    monkeypatch.setattr(web_server, "_STARTED", time.time() + 3600)     # nothing on disk is newer
    d = web_server.code_drift()
    assert d["stale_count"] == 0 and d["core_build"] in ("release", "debug", "installed", "none")
    monkeypatch.setattr(web_server, "_drift_cache", (0.0, {}))
    monkeypatch.setattr(web_server, "_STARTED", 0)                       # everything is newer
    d = web_server.code_drift()
    assert d["stale_count"] > 10 and all(p.startswith("aurora/") for p in d["stale"])
    st, body = call(app, "/health")
    assert "code" in body and "core_build" in body["code"]


def test_r509_restart_script_names_the_core():
    s = (Path(__file__).resolve().parent.parent / "deploy/restart-web.sh").read_text()
    assert "core_build" in s and "run ./install.sh" in s


# ── R510 (F2): connections are capped; over the cap a client gets a 503 without a thread ──
def test_r510_connection_cap(monkeypatch):
    monkeypatch.setattr(web_server, "MAX_CONNECTIONS", 5)

    class H(web_server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 5

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    before = threading.active_count()        # other tests may leave daemon threads behind
    srv = web_server.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(5)]
    try:
        for _ in range(50):
            if srv.connections == 5:
                break
            time.sleep(0.02)
        s = socket.create_connection(("127.0.0.1", port))
        s.settimeout(3)
        got = s.recv(4096)
        assert got.startswith(b"HTTP/1.1 503") and b"too many open connections" in got
        s.close()
        assert threading.active_count() <= before + 1 + 5 + 2
    finally:
        for s in idle:
            s.close()
    for _ in range(100):                     # the slots come back when the clients go
        if srv.connections == 0:
            break
        time.sleep(0.05)
    assert srv.connections == 0
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
        assert r.read() == b"ok"
    srv.shutdown()


# ── R510 (F3): media uploads are streamed to disk, never held whole ──
def test_r510_media_upload_is_streamed(app):
    size = 30_000_000
    data = b"\0" * size
    tracemalloc.start()
    st, _, body = _req(app, "/api/attach", {"X-Filename": "clip.mp4", "Content-Type": "application/octet-stream"}, data)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert st == 200, body
    import json
    res = json.loads(body)
    assert res["kind"] == "media" and Path(res["path"]).stat().st_size == size and "30.0 MB" in res["text"]
    # the request body is the client's copy; the server side may add a few MB of chunks, not another 30 MB
    assert peak < size + 8_000_000


def test_r510_save_stream_removes_a_partial_file(app):
    before = set(attach.input_dir().iterdir()) if attach.input_dir().is_dir() else set()
    with pytest.raises(OSError):
        attach.save_stream("x.mp4", io.BytesIO(b"abc"), 10)
    assert set(attach.input_dir().iterdir()) == before


# ── R510 (F4): files are streamed and Range is honoured ──
def test_r510_file_range(app):
    f = Path(attach.save_original("v.txt", b"0123456789"))
    st, h, body = _req(app, f"/api/files/file?name={f.name}")
    assert st == 200 and body == b"0123456789" and h["Accept-Ranges"] == "bytes"
    st, h, body = _req(app, f"/api/files/file?name={f.name}", {"Range": "bytes=2-4"})
    assert st == 206 and body == b"234" and h["Content-Range"] == "bytes 2-4/10"
    st, h, body = _req(app, f"/api/files/file?name={f.name}", {"Range": "bytes=-3"})
    assert st == 206 and body == b"789"
    st, h, _ = _req(app, f"/api/files/file?name={f.name}", {"Range": "bytes=50-"})
    assert st == 416 and h["Content-Range"] == "bytes */10"


# ── R510 (F3): the queue's pictures are capped ──
def test_r510_queue_picture_cap(app, monkeypatch):
    from aurora.web import core
    monkeypatch.setattr(core, "MAX_QUEUE_IMAGE_BYTES", 100)
    monkeypatch.setattr(app.s, "vision", lambda probe=False: True)
    app.s.busy = True
    try:
        ok, _ = app.s.send("one", ["data:image/png;base64," + "A" * 60])
        assert ok
        ok, why = app.s.send("two", ["data:image/png;base64," + "A" * 60])
        assert not ok and "queue is full" in why
        assert app.s.send("text only")[0]
    finally:
        app.s._queue.clear()
        app.s.busy = False


# ── R511 (F5): secrets inside values are hidden from the page and put back on save ──
def test_r511_inline_secrets_hidden_and_restored():
    from aurora import configedit as c
    t = ("providers:\n  a:\n    base_url: https://u:pass123@h/v1?x=1&api_key=QQQ\n"
         "mcp_servers:\n  - name: x\n    command: [\"npx\", \"--token=supersecret99\", \"--key-file=/x\"]\n"
         "    args:\n      - --password=hunter2\n      - plain\n")
    r = c.redact(t)
    for secret in ("pass123", "QQQ", "supersecret99", "hunter2"):
        assert secret not in r
    assert "https://u:" in r and "--key-file=/x" in r and "plain" in r
    assert c.restore(r, t) == t
    assert c.restore(r.replace("/x", "/y"), t) == t.replace("/x", "/y")


# ── R512 (F6): web_fetch resolves before sending, and a proxy does not switch the check off or block all ──
def test_r512_private_name_never_receives_the_request(monkeypatch):
    from aurora.extensions_bundled import web_extension as w
    from aurora import tools
    monkeypatch.setattr(tools, "_private_host", lambda h, resolve=True: False)       # the name looks public
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))])
    sent = []
    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: sent.append(a) or (_ for _ in ()).throw(AssertionError))
    out = w.web_fetch("https://rebind.example/")
    assert "blocked" in out and sent == []


def test_r512_proxy_detection(monkeypatch):
    from aurora.extensions_bundled import web_extension as w
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    assert w._proxied("https://example.com/") is False
    monkeypatch.setenv("https_proxy", "http://10.0.0.1:3128")
    monkeypatch.setenv("no_proxy", "internal.example")
    assert w._proxied("https://example.com/") is True
    assert w._proxied("https://internal.example/") is False
