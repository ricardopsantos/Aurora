"""MCP (Model Context Protocol) stdio client (R119) — lets Aurora call tools
exposed by any locally-configured MCP server (github, filesystem, fetch, …)
alongside its own built-in tools.

stdio transport only (no remote SSE/HTTP servers, for now): Aurora spawns
each configured server as a child process and speaks newline-delimited
JSON-RPC 2.0 over its stdin/stdout, per the MCP spec — one JSON object per
line, no LSP-style Content-Length framing.

Every `mcp_<server>_<tool>` call needs approval, no exceptions. MCP has no
universal "this tool is read-only/safe" flag, and Aurora's whole safety
model is approval-gated writes — silently trusting a configured server
would be exactly the gap that gate exists to close. See
`tools.needs_approval`.
"""

import atexit
import collections
import json
import os
import subprocess
import threading
import time

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "aurora", "version": "1"}
# R170h: what every MCP server gets even without an explicit `env:` entry in
# its config — just enough to run a normal program (find its binary, know
# where home/tmp are, behave sanely under a non-UTF-8 locale). NOT the rest
# of Aurora's own environment, which routinely holds its own provider API
# keys (OPENROUTER_API_KEY etc.) set by the user's shell — those used to be
# handed to every configured server for free via `{**os.environ, **env}`,
# so a compromised or merely misconfigured server (YAML the user edits, or
# templated from somewhere untrusted) could read them straight out of its
# own environment. A server that genuinely needs something else declares it
# in its own `env:` block, same as it always could.
#
# R171/S5: `HOME`/`USER`/`TMPDIR` still pass through verbatim — R170h's fix
# was scoped to secrets (API keys), not identity. A malicious/compromised
# MCP server still learns the real username and the exact temp-dir path it
# could later ask the model to read back from (indirect prompt injection via
# the filesystem). Not closed here: an MCP server is TRUSTED code the user
# configured, same trust level as a shell command or extension Aurora runs —
# env-filtering was never meant to be a sandbox boundary, only a
# secret-leak guard. Documented so that assumption isn't silently implied.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL",
                  "TERM")

# R170h/S5 guard the LEAK direction (Aurora's own secrets never reach a
# server's env unless named). This guards the opposite direction: a config's
# `env:` map is user-authored YAML — trusted, but still hand-edited and
# typo-prone — and `_child_env` used to merge it in unconditionally. These
# names aren't identity or secrets, they're CODE INJECTION into the child on
# its next dynamic-link/interpreter startup: `env: {LD_PRELOAD: SOME_KEY}`
# resolves a keystore value verbatim into the loader var, or a fat-fingered
# `env: {LD_PRELOAD: /nonexistent}` (meant as a literal, not a keystore
# lookup) silently does the same via whatever `_resolve_env` happens to
# return. No legitimate MCP server config needs to SET one of these on its
# own child — always stripped, never just filtered by allowlist membership,
# so a user typo can't reintroduce it the way it could for HOME/USER above.
# R239: the interpreter entries were incomplete, and lopsidedly so — Perl's
# PATH variable (PERL5LIB) and Ruby's OPTION variable (RUBYOPT) were listed,
# but for each language only one of that pair was. The gaps below are all
# code execution in the child, and the Python ones matter most: a stdio MCP
# server is very often a Python process (this repo's own `agentic_context_mcp`
# entry runs one), and `PYTHONSTARTUP` — the only Python name previously
# listed — is read ONLY in interactive mode, so it does nothing to a spawned
# server. `PYTHONPATH` prepends a directory to `sys.path`, so a module placed
# there shadows a stdlib or third-party one and runs at import, before the
# server's own first line. Verified end to end: a `json.py` on PYTHONPATH
# executed in a child running `python3 -c "import json"`.
_ENV_DENYLIST = ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
                 "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
                 "DYLD_FRAMEWORK_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
                 "DYLD_FALLBACK_FRAMEWORK_PATH", "BASH_ENV", "ENV",
                 "PYTHONSTARTUP", "PYTHONPATH", "PYTHONHOME",
                 "PYTHONEXECUTABLE", "NODE_OPTIONS", "PERL5LIB", "PERL5OPT",
                 "PERLLIB", "RUBYOPT", "RUBYLIB", "CLASSPATH",
                 "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
                 "GCONV_PATH")


def _child_env(env: dict[str, str] | None) -> dict[str, str]:
    """Not to be confused with the module-level `_resolve_env` below, which
    resolves a config's `env: {CHILD_VAR: AURORA_KEYSTORE_VAR}` mapping
    through the keystore — this is the LATER step, turning that already-
    resolved dict into the actual env passed to `Popen`."""
    base = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    if env:
        base.update(env)
    for k in _ENV_DENYLIST:
        base.pop(k, None)
    return base
# R153: how much of a server's stderr to keep, and how much to quote back in
# an error. Bounded on BOTH axes — a chatty server must not grow Aurora's
# memory, and a server that dies after logging 500 lines must not paste all
# of them into a one-line warning on the status surface.
_STDERR_KEEP_LINES = 50
_STDERR_QUOTE_LINES = 5
_STDERR_QUOTE_CHARS = 400

# Per-request ceiling when a server's config doesn't set `timeout:`. Generous
# enough for a handshake and an ordinary tool call, but NOT for every server:
# one that shells out to real work needs more (agentic_context_mcp allows its
# scripts 30s), and since crossing this kills the child with no reconnect,
# the config override is the difference between a slow call and a server
# that's gone for the rest of the session. See MCPManager.__init__.
_DEFAULT_TIMEOUT = 15.0

# Absolute ceiling on ONE request, as a multiple of `timeout`. The per-read
# deadline reset (see _read_response) deliberately lets a server that keeps
# talking keep its allowance — but with no upper bound, a server emitting
# notifications in a loop and never the reply blocks the turn thread forever.
_MAX_TOTAL_WAIT_MULT = 8

# R197: ceiling on the unparsed read buffer for ONE response. `_read_response`
# accumulates 64KB chunks until it finds a newline, so a server that sends a
# very long line — or never terminates one at all — grows this without limit.
# The time caps above don't bound it: they permit up to
# `timeout * _MAX_TOTAL_WAIT_MULT` seconds of a pipe running at memory speed.
# Same unbounded-producer shape as R194, and not exotic here: one JSON-RPC
# line IS how a large MCP tool result arrives.
#
# 64MB is far above any legitimate response (`run_tool` truncates the parsed
# text to TOOL_OUTPUT_LIMIT = 60k anyway) while still bounding the damage.
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class MCPServerError(Exception):
    pass


def _brief(value, limit: int = 120) -> str:
    """A repr bounded for use in a warning on the status surface — same
    reasoning as `_STDERR_QUOTE_CHARS`: a server that sends something huge
    and malformed must not paste all of it into a one-line warning."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


class MCPServer:
    """One running MCP server child process (stdio transport). Blocking,
    one request in flight at a time — matches how Aurora already calls
    tools sequentially per turn (MCP tools are never in
    `tools.PARALLEL_SAFE`, so nothing else calls into this concurrently)."""

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                env: dict[str, str] | None = None,
                timeout: float = _DEFAULT_TIMEOUT):
        self.name = name
        self.timeout = timeout
        self.tools: list[dict] = []
        # Non-fatal complaints about this server: malformed `tools/list`
        # entries dropped rather than raised (see _discover_tools), and a
        # protocol-version disagreement (see _initialize). Collected by
        # MCPManager into the same `errors` surface a failed server startup
        # already reports through.
        self.warnings: list[str] = []
        self._id = 0
        self._lock = threading.Lock()
        # R126: BINARY pipes with our own line buffering (`self._buf`), not
        # text mode. `select` reports readability of the file DESCRIPTOR,
        # while a TextIOWrapper's `readline()` reads from its own internal
        # buffer — so a line already sitting in that buffer is invisible to
        # `select`, and the deadline loop below would wait out the full
        # timeout on a reply it had already received. See _read_response.
        self._buf = bytearray()
        # R153: see _drain_stderr. Created before Popen so the accessors are
        # safe even if the spawn itself fails.
        self._stderr_lines: collections.deque[str] = collections.deque(
            maxlen=_STDERR_KEEP_LINES)
        self._stderr_lock = threading.Lock()
        try:
            self._proc = subprocess.Popen(
                [command, *(args or [])], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, env=_child_env(env))
        except OSError as e:
            raise MCPServerError(
                f"mcp server {name!r}: failed to start {command!r}: {e}") from e
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name=f"mcp-stderr-{name}", daemon=True)
        self._stderr_thread.start()
        # R145a: the handshake must not leak the child it just spawned. Only
        # the TIMEOUT path inside _read_response kills; every other failure
        # here — a JSON-RPC `error` reply (a protocol-version mismatch, say),
        # "closed the connection", a BrokenPipeError — used to propagate out
        # of __init__ with the process still running. And because __init__
        # raised, MCPManager never stored the object, so close_all/atexit
        # could not reach it either: one live child plus its pipes leaked for
        # the whole session, every session, for a misconfigured server.
        try:
            self._initialize()
            self._discover_tools()
        except BaseException:
            self._kill_unresponsive()
            raise

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _drain_stderr(self) -> None:
        """R153: keep the last `_STDERR_KEEP_LINES` lines the child wrote to
        stderr, so a failure can say WHY instead of only that it happened.

        stderr used to go to `DEVNULL`. The spawn-failure path is fine
        without it (the `OSError` carries "No such file or directory"), but
        every failure AFTER the process starts — a server that imports a
        missing dependency and dies mid-handshake, one that refuses the
        protocol version, one that wedges — produced `connection closed` or
        `timed out` with the child's own traceback thrown away. Diagnosing a
        misconfigured server meant re-running its command by hand outside
        Aurora.

        It MUST be a thread, not a read at failure time. A pipe nobody
        drains fills its OS buffer (~64KB) and then blocks the child on its
        next `write` — a chatty server would deadlock instead of running.
        Daemon, and it ends on its own at EOF when the child exits."""
        stream = self._proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if text:
                    with self._stderr_lock:
                        self._stderr_lines.append(text)
        except Exception:
            pass          # the pipe closed under us — nothing left to read
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _stderr_tail(self) -> str:
        """The last few stderr lines, formatted for appending to an error
        message — bounded in lines AND characters, empty when the server
        said nothing."""
        # When the child is already gone, the last thing it wrote — usually
        # the traceback that explains the failure — may still be in flight
        # in the drain thread. Wait briefly for EOF rather than racing it.
        if self._proc.poll() is not None:
            self._stderr_thread.join(timeout=0.2)
        with self._stderr_lock:
            lines = list(self._stderr_lines)[-_STDERR_QUOTE_LINES:]
        text = " / ".join(lines).strip()
        if not text:
            return ""
        if len(text) > _STDERR_QUOTE_CHARS:
            text = "…" + text[-_STDERR_QUOTE_CHARS:]
        return f" (stderr: {text})"

    def _fail(self, detail: str) -> MCPServerError:
        """Every post-spawn failure goes through here so the child's stderr
        is attached uniformly (R153) — returned, not raised, so call sites
        keep their `raise ... from e` chaining."""
        return MCPServerError(
            f"mcp server {self.name!r}: {detail}{self._stderr_tail()}")

    def _write_all(self, data: bytes) -> None:
        """`self._proc.stdin` is a RAW, unbuffered stream (`bufsize=0`, R126 —
        needed so `flush()` isn't a silent no-op the way it is on a raw
        stream anyway, but mainly to keep stdin/stdout symmetric with the
        line-buffering `_read_response` does itself). Unlike a
        `BufferedWriter`, a raw stream's `write()` is explicitly allowed to
        return FEWER bytes than given — a "short write" — most commonly when
        the payload is larger than the pipe's buffer and the child isn't
        draining fast enough. `BufferedWriter.flush()` loops internally until
        its buffer is empty; a raw stream does not, so skipping this loop
        would let a large request (a long patch, a big JSON blob as a tool
        argument) get silently truncated mid-write. The child then sees
        malformed JSON and either replies with a parse error or produces
        nothing at all — which reads as a hung/broken server, not as what
        actually happened here.

        Review pass, found by re-reading R126's stdin pipe change against
        Python's io semantics — no bug report triggered this one, so there
        is no regression test proving the OLD code was broken (a single
        `write()` call can't be forced to short-write from a test without
        a real oversized pipe); the test that exists instead proves this
        loop still sends everything given a `write()` that short-writes on
        purpose."""
        view = memoryview(data)
        while view:
            n = self._proc.stdin.write(view)
            if not n:      # 0 or None: nothing written this call, try again
                continue
            view = view[n:]

    def _send(self, method: str, params: dict | None = None,
             *, notification: bool = False) -> dict | None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notification:
            msg["id"] = self._next_id()
        if self._proc.stdin is None or self._proc.stdout is None:
            raise self._fail("no stdio pipes")
        try:
            self._write_all((json.dumps(msg) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            # the child already exited (closed(), or crashed on its own) —
            # a plain write/flush error here would otherwise surface as a
            # raw traceback instead of the same MCPServerError every other
            # failure mode in this class produces
            raise self._fail(f"connection closed: {e}") from e
        if notification:
            return None
        return self._read_response(msg["id"])

    def _read_response(self, want_id: int) -> dict:
        # tolerate stray lines (logging, notifications) that don't carry a
        # matching "id" — only a well-formed matching reply satisfies this.
        #
        # Bug fix: `readline()` is a BLOCKING call — a `while time.monotonic()
        # < deadline` loop around it only re-checks the deadline BETWEEN
        # lines, so a server that accepts the request and then produces NO
        # output at all (hung, deadlocked) blocked here forever; `self.timeout`
        # was never actually enforced against a silent server, only a slow
        # one. Since MCPServer() runs during Engine construction
        # (extensions.discover() → mcp_extension.register()), a single wedged
        # server used to hang Aurora's entire startup with no way out.
        # `select.select` enforces the deadline on the wait itself, not just
        # between reads.
        #
        # R126: but `select` alone is not enough, and the original fix was
        # subtly wrong. It polls the file DESCRIPTOR, while the old text-mode
        # `readline()` read from the TextIOWrapper's own internal buffer. An
        # MCP server routinely emits a notification line and its reply in one
        # burst, so a single OS read pulls BOTH into that buffer: the first
        # `readline()` returned the notification, the loop selected again, the
        # fd had nothing left to report, and the client waited out the whole
        # timeout — then killed a server whose reply was already in memory.
        # (The same mismatch also broke a server that emits a partial line and
        # then stalls.) So: binary pipes, and do the line buffering HERE, where
        # a buffered line is checked BEFORE ever waiting on the fd again.
        import select
        deadline = time.monotonic() + self.timeout
        hard_deadline = time.monotonic() + self.timeout * _MAX_TOTAL_WAIT_MULT
        while True:
            line = self._next_buffered_line()
            if line is not None:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue      # stray non-JSON output (logging) — skip
                if msg.get("id") == want_id:
                    if "error" in msg:
                        raise self._fail(f"{msg['error']}")
                    return msg.get("result", {})
                continue          # a notification or another id — keep reading
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_unresponsive()
                raise self._fail("timed out waiting for a response")
            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self._proc.stdout.fileno(), 65536)
            if not chunk:
                raise self._fail("closed the connection")
            self._buf += chunk
            # R197: bound the UNPARSED buffer, not just the wait. Everything
            # above caps how long a server may take; nothing capped how much
            # it may send before completing a single line.
            if len(self._buf) > _MAX_RESPONSE_BYTES:
                self._kill_unresponsive()
                raise self._fail(
                    f"response exceeded {_MAX_RESPONSE_BYTES} bytes without a "
                    "complete line")
            # R187e: the per-read reset below judges liveness by "is data
            # still arriving", which a server can satisfy forever — a
            # notifications/progress loop that never sends the matching reply
            # kept this blocked with no ceiling, and it runs on the turn's
            # own thread. The absolute cap bounds that without weakening the
            # intent: a server doing real work still gets its full per-read
            # allowance, up to a total generous enough (_MAX_TOTAL_WAIT_MULT
            # x timeout) that only a genuinely stuck stream reaches it.
            if time.monotonic() >= hard_deadline:
                self._kill_unresponsive()
                raise self._fail(
                    f"still sending data but no reply to this request after "
                    f"{self.timeout * _MAX_TOTAL_WAIT_MULT:g}s")
            # Review pass: the deadline was computed once and never touched
            # again, so a server that's genuinely alive and doing real work —
            # streaming `notifications/progress` lines while a slow tool call
            # runs — got killed for taking longer than `self.timeout` even
            # though it kept producing output the whole time. R126's own
            # rationale ("don't kill a healthy server") argues for judging
            # liveness by "is data still arriving," not "has the wall clock
            # crossed a single fixed line" — so any successfully read chunk
            # (even a stray notification, not yet the matching reply) resets
            # the clock. A truly silent/hung server never reaches this line
            # and still times out exactly as before.
            deadline = time.monotonic() + self.timeout

    def _next_buffered_line(self) -> bytes | None:
        """Pop one complete newline-terminated line out of `self._buf`, or
        None when the buffer holds no full line yet. Keeping the buffer here
        (rather than inside a TextIOWrapper) is what makes the deadline in
        `_read_response` actually correct — see the note there."""
        nl = self._buf.find(b"\n")
        if nl == -1:
            return None
        line = bytes(self._buf[:nl])
        del self._buf[:nl + 1]
        return line

    def _close_pipes(self) -> None:
        """R187e: stdin/stdout were never closed — only stderr, by the drain
        thread. Two descriptors per server therefore stayed open until the
        Popen object was garbage collected, which for a long session with
        several configured servers is a slow fd leak against the process
        limit. Both teardown paths (`close`, `_kill_unresponsive`) end here."""
        for pipe in (self._proc.stdin, self._proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass

    def _kill_unresponsive(self) -> None:
        """A timed-out server gets killed rather than left running — same
        reasoning as tools.py's grep timeout: an unresponsive child left
        alive is a leaked/zombie process, and a future call would just hang
        again waiting on the same wedged process."""
        try:
            self._proc.kill()
            self._proc.wait(timeout=3)
        except Exception:
            pass
        self._close_pipes()

    def _initialize(self) -> None:
        with self._lock:
            result = self._send("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO})
            self._send("notifications/initialized", notification=True)
        # R187e: the handshake result used to be discarded outright, so a
        # server answering with a different protocol version looked identical
        # to one that agreed — and the mismatch surfaced later as whatever
        # unrelated-looking symptom it happened to cause (a missing field, an
        # empty tool list). Recorded, NOT enforced: the spec expects a client
        # to accept a server's version or fail, but Aurora can't know which
        # differences matter, and refusing a server that then works fine
        # would be a worse regression than the ambiguity. Servers are
        # user-configured and mostly work; this just makes the disagreement
        # visible in the same warnings surface everything else uses.
        served = (result or {}).get("protocolVersion")
        if isinstance(served, str) and served != _PROTOCOL_VERSION:
            self.warnings.append(
                f"speaks MCP {served}, client requested {_PROTOCOL_VERSION} "
                f"— proceeding, but treat protocol-shaped oddities as suspect")

    def _discover_tools(self) -> None:
        """Keep only the tool entries Aurora can actually build a spec from,
        recording why any were dropped (`self.warnings`).

        `tools/list` output is a SERVER's data, not ours, and it used to be
        stored verbatim — so `_to_aurora_spec`'s `mcp_tool['name']` raised
        KeyError on an entry without a name. That raise happens in
        `specs()`/`runners()`, which run in `mcp_extension.register()`, well
        after `MCPManager.__init__`'s per-server try/except has finished — so
        the "one bad server must never take down the others" guarantee that
        constructor exists to provide did not hold: the whole extension
        failed to register, taking every OTHER server's tools with it, and
        the resulting warning named no server. Validating here, at the point
        the data arrives, keeps the failure per-server and per-tool."""
        with self._lock:
            result = self._send("tools/list", {})
        entries = result.get("tools", [])
        if not isinstance(entries, list):
            raise self._fail(
                f"tools/list returned {type(entries).__name__}, expected a list")
        kept: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                self.warnings.append(
                    f"skipped a tools/list entry that isn't an object: "
                    f"{_brief(entry)}")
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                self.warnings.append(
                    f"skipped a tool with no usable name: {_brief(entry)}")
                continue
            kept.append(entry)
        self.tools = kept

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        with self._lock:
            result = self._send("tools/call",
                                {"name": tool_name, "arguments": arguments})
        parts = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(f"[{block.get('type', 'unknown')} content]")
        text = "\n".join(parts)
        return f"[mcp tool error: {text}]" if result.get("isError") else text

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
                # R187e: the wait() was missing here, so a child that ignored
                # SIGTERM got killed and then never reaped — a zombie holding
                # a process-table slot until Aurora itself exited.
                # `_kill_unresponsive` always did this correctly; this path
                # didn't.
                self._proc.wait(timeout=3)
            except Exception:
                pass
        self._close_pipes()
        # R153: the drain thread ends at EOF once the child is gone, and it
        # closes the stderr pipe itself. Joined only so a caller that closes
        # every server in a loop isn't left with threads still winding down.
        try:
            self._stderr_thread.join(timeout=1)
        except Exception:
            pass


def _to_aurora_spec(server_name: str, mcp_tool: dict) -> dict:
    """Translate an MCP tool definition into Aurora's SPEC shape. Name-
    prefixed with the server so two servers can't register colliding tool
    names (e.g. both exposing a plain "search")."""
    return {
        "name": f"mcp_{server_name}_{mcp_tool['name']}",
        "description": ((mcp_tool.get("description") or "").strip()
                        + f" (via MCP server '{server_name}')"),
        "parameters": mcp_tool.get("inputSchema")
                     or {"type": "object", "properties": {}},
    }


def _make_runner(server: MCPServer, mcp_tool_name: str):
    def runner(**kwargs):
        return server.call_tool(mcp_tool_name, kwargs)
    return runner


class MCPManager:
    """Owns every configured MCP server's connection for the Engine's
    lifetime. `specs()`/`runners()` feed straight into `tools.py` the same
    way `context.py` and the bundled extensions already do."""

    def __init__(self, configs: list[dict]):
        self._servers: dict[str, MCPServer] = {}
        self.errors: list[str] = []
        for c in configs:
            name, command = c.get("name"), c.get("command")
            if not name or not command:
                self.errors.append(
                    f"skipped a mcp_servers entry missing name/command: {c!r}")
                continue
            # config.yaml is machine-agnostic content shared across hosts with
            # different home layouts (e.g. macOS ~/Desktop/GitTea/... vs Linux
            # ~/repositories/...); Popen never does shell-style ~/$VAR
            # expansion, so a literal path here only ever worked on the
            # machine it was written on. Expand both here so the same
            # command/args can use ~ or $HOME and travel between machines.
            command = os.path.expanduser(os.path.expandvars(command))
            args = [os.path.expanduser(os.path.expandvars(a))
                    for a in (c.get("args") or [])]
            # Rejected BEFORE spawning, not after. `self._servers[name] = ...`
            # silently overwrote the earlier entry, dropping the only
            # reference to a child that was already spawned AND handshaked —
            # and since close_all() iterates the dict's values, neither it nor
            # the atexit hook could ever reach it, so it outlived the session.
            # (Same leak class as R145a, reached by a different route: there
            # the object was never stored because __init__ raised, here it was
            # stored and then displaced.) Refusing up front also keeps the
            # tool namespace honest — two servers sharing a name produce
            # colliding `mcp_<name>_<tool>` specs, so one set was unreachable
            # regardless of which server won the dict slot.
            if name in self._servers:
                self.errors.append(
                    f"{name}: duplicate mcp_servers name — skipped this "
                    f"entry; names must be unique (they prefix every tool as "
                    f"mcp_<name>_<tool>)")
                continue
            env = _resolve_env(c.get("env") or {})
            # `timeout:` was supported by MCPServer from the start but never
            # forwarded from config, so the 15s default was unreachable — and
            # a server whose own work legitimately runs longer got killed
            # (_kill_unresponsive) with no reconnect, i.e. dead for the rest
            # of the session on its first slow call. agentic_context_mcp
            # allows its scripts 30s by design, so the two ceilings were in
            # direct conflict with no way for the user to resolve it.
            timeout = c.get("timeout")
            try:
                timeout = _DEFAULT_TIMEOUT if timeout is None else float(timeout)
                if timeout <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self.errors.append(
                    f"{name}: ignoring invalid timeout {c.get('timeout')!r} "
                    f"(want a positive number of seconds), using "
                    f"{_DEFAULT_TIMEOUT:g}s")
                timeout = _DEFAULT_TIMEOUT
            try:
                server = MCPServer(name, command, args, env,
                                   timeout=timeout)
            except Exception as e:
                # one bad server must never take down the others, or Aurora
                # itself — same "a tool must not kill the turn" principle
                # (R42), applied here at startup instead of per-call
                self.errors.append(f"{name}: {e.__class__.__name__}: {e}")
                continue
            self._servers[name] = server
            # A server that connected fine can still have sent unusable tool
            # entries; surface those per-server rather than losing them.
            self.errors += [f"{name}: {w}" for w in server.warnings]
        if self._servers:
            atexit.register(self.close_all)

    def _unique_tools(self) -> list:
        """R305: `mcp_{server}_{tool}` is ambiguous (server `a_b` + tool `c`
        and server `a` + tool `b_c` are both `mcp_a_b_c`). specs() kept the
        first and runners() the last, so the gate showed one tool and the
        call ran another. A colliding name is dropped from BOTH, warned."""
        seen: dict[str, list] = {}
        for name, server in self._servers.items():
            for t in server.tools:
                seen.setdefault(f"mcp_{name}_{t['name']}", []).append((name, server, t))
        out = []
        for full, hits in seen.items():
            if len(hits) > 1:
                msg = (f"tool name {full} is ambiguous across servers "
                       f"{', '.join(h[0] for h in hits)} — skipped")
                if msg not in self.errors:
                    self.errors.append(msg)
                continue
            out.append((full, *hits[0]))
        return out

    def specs(self) -> list[dict]:
        return [_to_aurora_spec(name, t) for _f, name, _s, t in self._unique_tools()]

    def runners(self) -> dict:
        return {full: _make_runner(server, t["name"])
                for full, _n, server, t in self._unique_tools()}

    def close_all(self) -> None:
        for s in self._servers.values():
            s.close()


def _resolve_env(env_map: dict[str, str]) -> dict[str, str]:
    """`env: {CHILD_VAR: AURORA_KEYSTORE_VAR}` — resolve each value through
    Aurora's keystore (env var → OS keyring → encrypted file), never a
    blocking prompt (`interactive=False`): a missing credential at MCP
    startup should mean "that server likely fails/reports auth error", not
    "Aurora hangs waiting for input during engine construction"."""
    from . import keystore
    resolved = {}
    for child_var, aurora_var in env_map.items():
        value = keystore.get_key(aurora_var, interactive=False)
        if value:
            resolved[child_var] = value
    return resolved
