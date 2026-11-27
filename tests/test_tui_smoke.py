"""R456: end-to-end smoke tests: the real TUI in a pseudo-terminal, against a
fake local model server, with assertions on what the screen shows.

Unit tests drive the TUI's objects; these drive the program the way a person
does (keys in, screen out), which is where the R455 bugs were found. Needs
`pyte` (`pip install -e .[dev]`); skipped without it, and on Windows."""
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pyte = pytest.importorskip("pyte")
if sys.platform == "win32":                                   # pragma: no cover
    pytest.skip("needs a pty", allow_module_level=True)
import fcntl
import pty
import struct
import termios

REPO = Path(__file__).resolve().parent.parent
COLS, ROWS = 110, 32
KEYS = {"<esc>": "\x1b", "<enter>": "\r", "<bs>": "\x7f", "<up>": "\x1b[A", "<down>": "\x1b[B"}


# ── fake local model server (llama.cpp-style: /props + OpenAI chat) ──────────
class _Model(BaseHTTPRequestHandler):
    slow_started = None          # set per server: threading.Event

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/props"):
            return self._json({"default_generation_settings": {"n_ctx": 8192},
                               "model_path": "/models/smoke-model.gguf", "n_ctx": 8192})
        if self.path.startswith("/v1/models"):
            return self._json({"data": [{"id": "smoke-model"}]})
        return self._json({"status": "ok"})

    def _chunk(self, delta, finish=None):
        c = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.flush()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        msgs = req.get("messages") or []
        last_user = next((m.get("content") for m in reversed(msgs) if m.get("role") == "user"), "")
        last_user = last_user if isinstance(last_user, str) else json.dumps(last_user)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if msgs and msgs[-1].get("role") == "tool":
            self._chunk({"role": "assistant", "content": "The note says "})
            self._chunk({"content": f"{msgs[-1].get('content', '').strip()[-40:]}.\n"}, None)
        elif "read the note" in last_user:
            self._chunk({"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "note.txt"})}}]})
            self._chunk({}, "tool_calls")
            self.wfile.write(b"data: [DONE]\n\n")
            return
        elif "slow" in last_user:
            self._chunk({"role": "assistant", "content": "starting"})
            type(self).slow_started.set()
            try:
                for _ in range(300):
                    time.sleep(0.1)
                    self._chunk({"content": "."})
            except OSError:
                return                      # the client hung up: cancelled
        else:
            self._chunk({"role": "assistant", "content": "SMOKE-REPLY ok\n"})
        self._chunk({}, "stop")
        self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 3}})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def backend():
    handler = type("Model", (_Model,), {"slow_started": threading.Event()})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], handler
    srv.shutdown()


# ── the TUI in a pty ─────────────────────────────────────────────────────────
class Screen:
    def __init__(self, tmp: Path, port: int):
        (tmp / "work").mkdir()
        (tmp / "work" / "note.txt").write_text("hello-smoke\n")
        cfg = tmp / "config.yaml"
        cfg.write_text(
            "providers:\n"
            "  local:\n"
            "    type: openai\n"
            f"    base_url: http://127.0.0.1:{port}/v1\n"
            "models:\n"
            "  - provider: local\n"
            "    model: local\n"
            "    tools: true\n"
            "runtime:\n"
            "  max_iterations: 4\n"
            "  web_search: false\n"
            "  timeout: 30\n"
            "  redact_secrets: false\n")
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AURORA")}
        env.update(TERM="xterm-256color", COLUMNS=str(COLS), LINES=str(ROWS),
                   AURORA_HOME=str(tmp / "home"), AURORA_CONFIG=str(cfg), HOME=str(tmp),
                   LLAMA_API_KEY="")
        # Popen, not pty.fork: forking a process that runs the fake server's
        # threads can deadlock the child
        fd, child = pty.openpty()
        fcntl.ioctl(child, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "aurora"], cwd=tmp / "work", env=env,
            stdin=child, stdout=child, stderr=child, start_new_session=True)
        os.close(child)
        self.fd = fd

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            self.stream.feed(data)
            if b"\x1b[6n" in data:                          # cursor-position request
                y, x = self.screen.cursor.y + 1, self.screen.cursor.x + 1
                os.write(self.fd, f"\x1b[{y};{x}R".encode())

    def text(self) -> str:
        return "\n".join(line.rstrip() for line in self.screen.display)

    def wait_for(self, pattern: str, timeout: float = 15) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.2)
            if re.search(pattern, self.text()):
                return self.text()
        raise AssertionError(f"never showed {pattern!r}; screen:\n{self.text()}")

    def type(self, *keys: str, gap: float = 0.15) -> None:
        for k in keys:
            os.write(self.fd, KEYS.get(k, k).encode())
            self.pump(gap)

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(10)
        os.close(self.fd)


@pytest.fixture
def run(tmp_path):
    started = []

    def _start(port):
        s = Screen(tmp_path, port)
        started.append(s)
        s.wait_for(r"session", 20)
        s.pump(1.0)
        return s
    yield _start
    for s in started:
        s.close()


def _ask(s: Screen, text: str) -> None:
    s.type(text, gap=0.05)
    s.type("<enter>")


# ── scenarios ────────────────────────────────────────────────────────────────
def test_start_with_backend_up_says_ready_and_answers(backend, run):
    s = run(backend[0])
    assert re.search(r"✔ .*ready", s.text()), s.text()
    _ask(s, "say something")
    s.wait_for("SMOKE-REPLY ok")


def test_start_with_backend_down_never_claims_ready(run):
    s = run(_free_port())
    screen = s.wait_for(r"Select model")      # a dead start model offers the picker
    assert "✘" in screen and not re.search(r"✔ .*ready", screen), screen
    assert re.search(r"\(current\).*⚠ unreachable", screen), screen   # R455 (#24)
    s.type("<esc>")
    s.wait_for(r"cancelled — model unchanged")
    _ask(s, "say something")
    screen = s.wait_for(r"local backend unreachable", 40)
    s.pump(1.0)
    screen = s.text()
    assert "thought for" not in screen, screen                    # R455 (#26)
    assert "· ·" not in screen, screen                           # R455 (#27)
    assert "llama.cpp" in screen                                  # the right advice (#25)


def test_one_turn_with_a_tool_call(backend, run):
    s = run(backend[0])
    _ask(s, "please read the note")
    s.wait_for(r"The note says .*hello-smoke", 30)


def test_bash_mode_runs_real_bash_and_shows_exit_codes(backend, run):
    s = run(backend[0])
    s.type("!")
    s.wait_for(r"bash mode")
    _ask(s, "echo {a,b}-brace")
    s.wait_for(r"a-brace b-brace")                               # bash, not dash
    _ask(s, "false")
    s.wait_for(r"\[exit 1\]")


def test_model_picker_opens_and_cancels_with_a_result(backend, run):
    s = run(backend[0])
    _ask(s, "/model")
    s.wait_for(r"Select model")
    assert "(current)" in s.text()                               # R455 (#24)
    s.type("<esc>")
    s.wait_for(r"cancelled — model unchanged")                   # R455 (#33)


def test_esc_esc_cancels_a_running_turn(backend, run):
    port, handler = backend
    s = run(port)
    _ask(s, "go slow please")
    assert handler.slow_started.wait(15), s.text()
    s.wait_for(r"starting")
    s.type("<esc>", "<esc>")
    s.wait_for(r"Cancel|cancel")
    s.type("<enter>")                                            # the default: Yes
    s.wait_for(r"interrupted|cancelled", 15)


def test_help_screen_is_the_short_tui_help(backend, run):
    s = run(backend[0])
    s.type("?")
    screen = s.wait_for(r"Basics")
    assert "Keys" in screen and "Classic REPL" not in screen


def test_command_menu_leaves_the_input_box_framed(backend, run):
    s = run(backend[0])
    s.type("/", "c", "o")
    s.wait_for(r"compact +summarize")
    lines = s.text().split("\n")
    prompt_row = max(i for i, l in enumerate(lines) if l.startswith("> /co"))
    assert set(lines[prompt_row - 1].strip()) == {"─"}, s.text()    # R455 (#31)
    assert set(lines[prompt_row + 1].strip()) == {"─"}, s.text()
    assert any("compact" in l for l in lines[:prompt_row - 1])      # the menu sits above
