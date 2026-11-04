"""R307 — a connection pool closed under a running request is replaced, not
retried on until the turn dies with "client has been closed"."""

import json
import socket
import threading

from aurora.providers import make_provider


def _server(on_first_chat):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    chats = [0]

    def handle(c):
        try:
            data = c.recv(65536)
        except OSError:
            return
        if b"chat/completions" not in data.split(b"\r\n")[0]:
            body = b'{"data":[{"id":"m"}]}'
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                      b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            c.close()
            return
        chats[0] += 1
        if chats[0] == 1:
            on_first_chat()        # the pool is closed while this request is in flight
            c.close()
            return
        ev = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        body = b"data: " + json.dumps(ev).encode() + b"\n\ndata: [DONE]\n\n"
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                  b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        c.close()

    def serve():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], chats


def test_turn_recovers_when_its_pool_is_closed_mid_request():
    holder = {}
    port, chats = _server(lambda: [c.close() for c in list(holder["p"]._http.values())])
    p = make_provider("local", {"type": "openai", "api_key": "k",
                                "base_url": [f"http://127.0.0.1:{port}/v1"]}, 30)
    holder["p"] = p
    p.notify = lambda _m: None
    r = p.turn("m", [{"role": "user", "content": "hi"}], "sys", [],
               lambda _t: None, lambda: False)
    assert r.text == "ok" and chats[0] == 2


def test_a_closed_cached_pool_is_never_handed_out():
    p = make_provider("local", {"type": "openai", "api_key": "k",
                                "base_url": ["http://127.0.0.1:9/v1"]}, 30)
    first = p._client_for("http://127.0.0.1:9/v1")
    first.close()
    second = p._client_for("http://127.0.0.1:9/v1")
    assert second is not first and not second.is_closed


def test_model_picker_probes_never_close_the_running_turns_pool(tmp_path):
    """R308: the /model picker probes every entry; opened mid-turn it used
    `_provider_for`, which rebuilt the turn's provider and closed its pool."""
    import yaml
    from aurora.engine import Engine
    cfg = {"providers": {"local": {"type": "openai", "base_url": "http://127.0.0.1:9/v1"},
                         "remote": {"type": "openai", "base_url": "http://127.0.0.1:8/v1",
                                    "api_key": "k"}},
           "models": [{"provider": "local", "model": "a"},
                      {"provider": "remote", "model": "b"}]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    eng = Engine(str(path))
    turn_provider = eng._provider_for(eng.current, interactive=True)
    pool = turn_provider._client_for(turn_provider.base_url)
    for m in eng.list_models():
        assert eng.probe_provider_for(m) is not None
    assert not pool.is_closed and eng._provider is turn_provider
