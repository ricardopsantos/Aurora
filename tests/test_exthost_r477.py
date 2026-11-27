"""R477: Python extensions behind a process boundary (aurora-ext/1), the layer Aurora 2.0's Rust core will use."""
import json
import os
import sys
import textwrap

import pytest

from aurora import exthost

EXT_STATIC = '''
from datetime import datetime, timezone
print("noise at import")                       # must not break the protocol
SPEC = [{"name": "echo", "description": "Echo it back.",
         "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
        {"name": "boom", "description": "Always fails.", "parameters": {"type": "object", "properties": {}}}]
def echo(text="", **_):
    print("noise in a call")
    return "echo: " + text
def boom(**_):
    raise ValueError("on purpose")
RUNNERS = {"echo": echo, "boom": boom}
'''
EXT_DYNAMIC = '''
def register(engine):
    greeting = engine.runtime.get("greeting", "hello")
    spec = [{"name": "greet", "description": "Greet.", "parameters": {"type": "object", "properties": {}}}]
    return spec, {"greet": lambda **_: f"{greeting} from {len(engine.cfg.get('models', []))} models"}
'''
EXT_BROKEN = "raise RuntimeError('cannot import me')\n"
EXT_DUP = '''
SPEC = [{"name": "echo", "description": "a second echo", "parameters": {"type": "object", "properties": {}}}]
RUNNERS = {"echo": lambda **_: "second"}
'''


@pytest.fixture
def exts(tmp_path):
    d = tmp_path / "extensions"
    d.mkdir()
    (d / "a_static.py").write_text(EXT_STATIC)
    (d / "b_dynamic.py").write_text(EXT_DYNAMIC)
    (d / "c_broken.py").write_text(EXT_BROKEN)
    (d / "d_dup.py").write_text(EXT_DUP)
    (d / "_private.py").write_text("raise SystemExit('never loaded')\n")
    return d


@pytest.fixture
def client(exts):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    c = exthost.Client(env=env, timeout=20)
    yield c
    c.close()


def _init(client, exts, cfg=None):
    return client.request("initialize", {"protocol": "aurora-ext/1", "paths": [str(exts)],
                                         "config": cfg or {"runtime": {"greeting": "olá"}, "models": [{}, {}]}})


def test_initialize_lists_tools_and_warns_about_bad_files(client, exts):
    r = _init(client, exts)["result"]
    assert r["protocol"] == "aurora-ext/1"
    assert [t["name"] for t in r["tools"]] == ["echo", "boom", "greet"]
    assert any("c_broken.py failed to load" in w for w in r["warnings"])
    assert any("echo already defined" in w for w in r["warnings"])            # first wins, as in process (R227)
    assert r["tools"][0]["parameters"]["required"] == ["text"]


def test_calls_return_output_and_errors_keep_the_host_alive(client, exts):
    _init(client, exts)
    assert client.request("tools/call", {"name": "echo", "arguments": {"text": "hi"}})["result"] == {
        "output": "echo: hi", "is_error": False}
    r = client.request("tools/call", {"name": "boom", "arguments": {}})["result"]
    assert r == {"output": "[tool error: ValueError: on purpose]", "is_error": True}
    assert client.request("tools/call", {"name": "greet", "arguments": {}})["result"]["output"] == "olá from 2 models"
    assert client.request("tools/call", {"name": "echo", "arguments": {"text": "still here"}})["result"]["output"] == \
        "echo: still here"


def test_protocol_errors(client, exts):
    assert client.request("tools/call", {"name": "echo", "arguments": {}})["error"]["code"] == -32002   # not initialized
    assert client.request("initialize", {"protocol": "aurora-ext/9", "paths": []})["error"]["code"] == -32602
    _init(client, exts)
    assert client.request("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602
    assert client.request("tools/call", {"name": "echo", "arguments": [1]})["error"]["code"] == -32602
    assert client.request("frobnicate")["error"]["code"] == -32601
    client.proc.stdin.write(b"{not json\n")
    client.proc.stdin.flush()
    assert json.loads(client.proc.stdout.readline())["error"]["code"] == -32700
    assert client.request("shutdown")["result"] == {}
    assert client.proc.wait(5) == 0


def test_same_tools_as_in_process(exts, monkeypatch, tmp_path):
    """The host serves exactly what the in-process loader gives the engine for the same files."""
    from aurora import extensions
    monkeypatch.setattr(extensions, "_dirs", lambda: [exts])
    specs, runners, warnings = extensions.discover(exthost.EngineView({"runtime": {"greeting": "olá"}, "models": []}))
    h = exthost.Host()
    h.load([str(exts)], {"runtime": {"greeting": "olá"}, "models": []})
    assert [s["name"] for s in h.specs] == list(dict.fromkeys(s["name"] for s in specs))
    assert h.runners["echo"](text="x") == runners["echo"](text="x")
    assert h.runners["greet"]() == runners["greet"]()


def test_a_bundled_extension_file_loads_in_the_host():
    """The shipped lint extension is a real-world SPEC/RUNNERS module: it must work behind the boundary."""
    from pathlib import Path
    import aurora
    f = Path(aurora.__file__).parent / "extensions_bundled" / "lint_extension.py"
    h = exthost.Host()
    warnings = h.load([str(f)], {})
    assert not warnings and h.specs
