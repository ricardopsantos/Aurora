"""R477: the Python extension host — Aurora's extensions behind a process boundary.

`python -m aurora.exthost` loads extension files (the same `SPEC` / `RUNNERS` /
`register(engine)` modules `aurora/extensions.py` loads in process) and serves
their tools over newline-delimited JSON-RPC 2.0 on stdin/stdout. Any host that
speaks the protocol can use them without embedding Python: this is how the
Rust core of Aurora 2.0 keeps today's extensions working unchanged, and how a
Python Aurora can run an extension it does not trust in its own process (and
sandbox, R475).

Protocol `aurora-ext/1` (documents/EXTENSIONS_PROTOCOL.md has the full text):

  → {"jsonrpc":"2.0","id":1,"method":"initialize",
     "params":{"protocol":"aurora-ext/1","paths":["~/.aurora/extensions"],
               "config":{...config.yaml...}}}
  ← {"jsonrpc":"2.0","id":1,"result":{"protocol":"aurora-ext/1",
     "tools":[{name, description, parameters}], "warnings":["..."]}}
  → {"jsonrpc":"2.0","id":2,"method":"tools/call",
     "params":{"name":"current_datetime","arguments":{}}}
  ← {"jsonrpc":"2.0","id":2,"result":{"output":"2026-11-25T...","is_error":false}}
  → {"jsonrpc":"2.0","id":3,"method":"shutdown"}
  ← {"jsonrpc":"2.0","id":3,"result":{}}

A tool that raises answers `is_error: true` with `[tool error: Type: message]`,
the text the in-process engine shows; the host keeps running. A malformed
request answers a JSON-RPC error (-32700 parse, -32600 request, -32601 method,
-32602 params). Anything an extension prints goes to stderr, never into the
protocol stream. `register(engine)` gets an `engine` with `cfg` (the config
sent in `initialize`) and `runtime` (`cfg["runtime"]`), nothing else: an
extension that needs more of the engine stays in-process only.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

PROTOCOL = "aurora-ext/1"
MAX_LINE = 16 * 1024 * 1024


class EngineView:
    """What `register(engine)` sees in the host: the config, read-only by contract."""

    def __init__(self, cfg: dict):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        rt = self.cfg.get("runtime")
        self.runtime = rt if isinstance(rt, dict) else {}
        # R494b: what the bundled extensions read of the in-process engine
        self.web = bool(self.runtime.get("web_search", True))
        ms = self.cfg.get("models")
        self.models = [m for m in ms if isinstance(m, dict)] if isinstance(ms, list) else []
        self.extension_warnings: list[str] = []
        self._mcp_manager = None


class Host:
    def __init__(self):
        self.specs: list[dict] = []
        self.runners: dict = {}
        self.ready = False

    # ── loading ──────────────────────────────────────────────────────────────
    def load(self, paths: list[str], cfg: dict) -> list[str]:
        from . import extensions
        warnings: list[str] = []
        files: list[Path] = []
        for p in paths:
            q = Path(os.path.expanduser(str(p)))
            if q.is_dir():
                files += [f for f in sorted(q.glob("*.py")) if not f.name.startswith("_")]
            elif q.suffix == ".py" and q.is_file():
                files.append(q)
            else:
                warnings.append(f"extension path {p} is not a .py file or a folder")
        engine = EngineView(cfg)
        for f in files:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    mod = extensions._load_module(f)
            except Exception as e:
                warnings.append(f"extension {f.name} failed to load: {e.__class__.__name__}: {e}")
                continue
            try:
                specs = list(getattr(mod, "SPEC", None) or [])
                runners = dict(getattr(mod, "RUNNERS", None) or {})
                reg = getattr(mod, "register", None)
                if reg is not None:
                    with contextlib.redirect_stdout(sys.stderr):
                        s2, r2 = reg(engine)
                    specs += list(s2 or [])
                    runners.update({k: v for k, v in (r2 or {}).items() if k not in runners})
            except Exception as e:
                warnings.append(f"extension {f.name} failed to register: {e.__class__.__name__}: {e}")
                continue
            for s in specs:                       # first wins, as in process (R227)
                name = s.get("name") if isinstance(s, dict) else None
                if not isinstance(name, str) or name in self.runners or name not in runners:
                    if isinstance(name, str) and name in self.runners:
                        warnings.append(f"extension {f.name}: tool {name} already defined, skipped")
                    elif isinstance(name, str):
                        warnings.append(f"extension {f.name}: tool {name} has no runner, skipped")
                    continue
                self.specs.append({"name": name, "description": str(s.get("description", "")),
                                   "parameters": s.get("parameters") or {"type": "object", "properties": {}}})
                self.runners[name] = runners[name]
        if engine._mcp_manager is not None:
            warnings += [f"mcp: {e}" for e in engine._mcp_manager.errors]
        return warnings + engine.extension_warnings

    # ── requests ─────────────────────────────────────────────────────────────
    def handle(self, req) -> dict | None:
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return _err(req.get("id") if isinstance(req, dict) else None, -32600, "invalid request")
        rid, method, params = req.get("id"), req["method"], req.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            if params.get("protocol", PROTOCOL) != PROTOCOL:
                return _err(rid, -32602, f"protocol {params.get('protocol')!r} not supported; this host speaks {PROTOCOL}")
            paths = params.get("paths")
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                return _err(rid, -32602, "paths: a list of file or folder paths")
            warnings = self.load(paths, params.get("config") or {})
            self.ready = True
            return _ok(rid, {"protocol": PROTOCOL, "tools": self.specs, "warnings": warnings})
        if method == "shutdown":
            return _ok(rid, {})
        if not self.ready:
            return _err(rid, -32002, "initialize first")
        if method == "tools/list":
            return _ok(rid, {"tools": self.specs})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in self.runners:
                return _err(rid, -32602, f"no such tool: {name!r}")
            if not isinstance(args, dict):
                return _err(rid, -32602, "arguments: an object")
            try:
                with contextlib.redirect_stdout(io.StringIO()) as junk:
                    out = self.runners[name](**args)
                stray = junk.getvalue()
                if stray:
                    sys.stderr.write(stray)
                return _ok(rid, {"output": out if isinstance(out, str) else json.dumps(out, default=str),
                                 "is_error": False})
            except Exception as e:
                return _ok(rid, {"output": f"[tool error: {e.__class__.__name__}: {e}]", "is_error": True})
        return _err(rid, -32601, f"no such method: {method}")


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve(inp=None, out=None) -> int:
    """Read requests line by line until EOF or `shutdown`."""
    inp = inp or sys.stdin.buffer
    out = out or sys.stdout.buffer
    host = Host()
    while True:
        line = inp.readline(MAX_LINE + 1)
        if not line:
            return 0
        if not line.strip():
            continue
        try:
            req = json.loads(line) if len(line) <= MAX_LINE else None
            resp = host.handle(req) if req is not None else _err(None, -32600, "request too large")
        except ValueError:
            resp = _err(None, -32700, "parse error")
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
            out.flush()
        if isinstance(req, dict) and req.get("method") == "shutdown":
            return 0


class Client:
    """A Python-side client (used by the tests, and by anything that wants an extension out of process)."""

    def __init__(self, argv: list[str] | None = None, env: dict | None = None, timeout: float = 30.0):
        import subprocess
        import tempfile
        self.timeout = timeout
        # R547: an extension's prints go to the host's stderr; an unread pipe filled at 64 kB and the host hung
        self._log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(argv or [sys.executable, "-m", "aurora.exthost"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._log, env=env)
        self._id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        import select
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()
        r, _, _ = select.select([self.proc.stdout], [], [], self.timeout)
        if not r:
            self.close()
            raise TimeoutError(f"extension host did not answer {method} in {self.timeout:g} s")
        line = self.proc.stdout.readline()
        if not line:
            self._log.seek(0)
            raise ConnectionError("extension host exited: " + self._log.read().decode("utf-8", "replace")[-500:])
        return json.loads(line)

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(5)
        except Exception:
            pass
        try:
            self._log.close()
        except Exception:
            pass


def main() -> int:
    # The protocol gets its own copy of stdout; fd 1 itself becomes stderr, so nothing an extension prints (a thread,
    # a C library, a subprocess) can land in the middle of a JSON-RPC line
    proto = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return serve(sys.stdin.buffer, proto)


if __name__ == "__main__":
    sys.exit(main())
