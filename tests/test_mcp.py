"""Tests for R119: MCP client (aurora/mcp.py), the extension mechanism
(aurora/extensions.py), and their wiring into tools.py/engine.py. No
network, no npm — the "server" is tests/fixtures/fake_mcp_server.py, a real
subprocess speaking the actual protocol, not a mock."""

import sys
import textwrap
import time
from pathlib import Path

import pytest

from aurora import extensions, mcp, tools

_FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
_HANGING_SERVER = str(Path(__file__).parent / "fixtures" / "hanging_mcp_server.py")
_CHATTY_SERVER = str(Path(__file__).parent / "fixtures" / "chatty_mcp_server.py")
_REFUSING_SERVER = str(Path(__file__).parent / "fixtures" / "refusing_mcp_server.py")
_NOISY_SERVER = str(Path(__file__).parent / "fixtures" / "noisy_mcp_server.py")
_ENV_SERVER = str(Path(__file__).parent / "fixtures" / "env_mcp_server.py")
_MALFORMED_SERVER = str(Path(__file__).parent / "fixtures"
                        / "malformed_tools_mcp_server.py")
_BABBLING_SERVER = str(Path(__file__).parent / "fixtures"
                       / "babbling_mcp_server.py")
_FIREHOSE_SERVER = str(Path(__file__).parent / "fixtures"
                       / "firehose_mcp_server.py")


def _child_python_count() -> int:
    """Live fixture-server processes, for leak assertions. `pgrep -f` on the
    fixtures directory rather than a PID list, so it catches a child whose
    only reference was dropped (see the duplicate-name test)."""
    import subprocess
    out = subprocess.run(
        ["pgrep", "-f", str(Path(__file__).parent / "fixtures")],
        capture_output=True, text=True).stdout
    return len([line for line in out.split() if line.strip()])


# ── aurora.mcp: the client itself, against a real (fake) server process ────
def test_mcp_server_discovers_and_calls_a_tool():
    server = mcp.MCPServer("fake", sys.executable, [_FAKE_SERVER])
    try:
        assert [t["name"] for t in server.tools] == ["echo"]
        assert server.call_tool("echo", {"text": "hi"}) == "echo: hi"
    finally:
        server.close()


def test_mcp_server_does_not_inherit_auroras_own_secrets(monkeypatch):
    """R170h: a spawned server used to get Aurora's ENTIRE os.environ via
    `{**os.environ, **env}` — including Aurora's own provider API keys, if
    set as env vars. A compromised or merely misconfigured server (YAML the
    user edits, or templated from an untrusted source) could read them
    straight out of its own environment. Now only an explicit allowlist
    (PATH/HOME/LANG/...) plus whatever the server's own config declares."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-not-for-you")
    server = mcp.MCPServer("envcheck", sys.executable, [_ENV_SERVER])
    try:
        assert server.call_tool("getenv", {"name": "OPENROUTER_API_KEY"}) == "<unset>"
        # the allowlist itself must still work, or every server would break
        assert server.call_tool("getenv", {"name": "PATH"}) != "<unset>"
    finally:
        server.close()


def test_mcp_server_still_gets_its_own_configured_env(monkeypatch):
    """The allowlist is a floor, not a ceiling — a server's own `env:`
    config (already resolved through the keystore by module-level
    `_resolve_env`, upstream of this) must still land in its process."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-not-for-you")
    server = mcp.MCPServer("envcheck", sys.executable, [_ENV_SERVER],
                           env={"MY_SERVER_TOKEN": "configured-value"})
    try:
        assert server.call_tool("getenv", {"name": "MY_SERVER_TOKEN"}) == "configured-value"
        assert server.call_tool("getenv", {"name": "OPENROUTER_API_KEY"}) == "<unset>"
    finally:
        server.close()


def test_mcp_server_config_cannot_inject_a_loader_var_into_the_child():
    """A config's `env:` map is user-authored YAML, hand-edited and
    typo-prone — `env: {LD_PRELOAD: SOME_KEY}` (or a keystore lookup that
    happens to resolve there) must never land LD_PRELOAD/DYLD_INSERT_LIBRARIES/
    etc. in the child's real environment: those aren't identity leaks, they're
    code injection into a process the user thinks is just env-filtered."""
    server = mcp.MCPServer("envcheck", sys.executable, [_ENV_SERVER],
                           env={"LD_PRELOAD": "/tmp/evil.so",
                                "MY_SERVER_TOKEN": "configured-value"})
    try:
        assert server.call_tool("getenv", {"name": "LD_PRELOAD"}) == "<unset>"
        # the rest of the configured env must still land — this is a
        # denylist on a few dangerous names, not a lockdown of env: itself
        assert server.call_tool("getenv", {"name": "MY_SERVER_TOKEN"}) == "configured-value"
    finally:
        server.close()


def test_a_failed_handshake_does_not_leak_the_child_process():
    """R145a: only the TIMEOUT path killed the child. Every other handshake
    failure — a JSON-RPC error reply, a closed connection, a broken pipe —
    propagated out of __init__ with the process still running, and since
    __init__ raised, MCPManager never stored it, so close_all/atexit could
    not reach it either. One live child per misconfigured server, per
    session, forever."""
    import subprocess
    spawned = []
    real_popen = subprocess.Popen

    def _spy(*a, **k):
        p = real_popen(*a, **k)
        spawned.append(p)
        return p

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcp.subprocess, "Popen", _spy)
        with pytest.raises(mcp.MCPServerError):
            mcp.MCPServer("refusing", sys.executable, [_REFUSING_SERVER])

    assert len(spawned) == 1
    # the fixture sleeps 300s after refusing, so a exited process proves the
    # client killed it rather than orphaning it
    assert spawned[0].poll() is not None, \
        "handshake failure left the child running"


def test_a_dying_server_reports_its_own_stderr():
    """R153: stderr went to DEVNULL, so a server that starts and THEN dies
    (missing dependency, bad config) produced 'closed the connection' with
    the child's own explanation discarded — the user had to re-run the
    server command by hand to find out why."""
    with pytest.raises(mcp.MCPServerError) as excinfo:
        mcp.MCPServer("noisy", sys.executable, [_NOISY_SERVER])
    msg = str(excinfo.value)
    assert "closed the connection" in msg      # the old message is kept
    assert "stderr:" in msg
    assert "Cannot find module '@modelcontextprotocol/sdk'" in msg


def test_captured_stderr_is_bounded_in_lines_and_in_quoted_length():
    """A long-running chatty server must not grow Aurora's memory, and a
    server that logged 500 lines before failing must not paste all of them
    into a one-line warning."""
    server = mcp.MCPServer("flood", sys.executable, [_NOISY_SERVER, "--flood"])
    try:
        # wait for the whole flood to land, not just the first N lines —
        # sampling mid-stream would assert against a moving target
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (server._stderr_lines
                    and server._stderr_lines[-1].startswith("log line 0499")):
                break
            time.sleep(0.05)
        assert len(server._stderr_lines) == mcp._STDERR_KEEP_LINES
        tail = server._stderr_tail()
        assert tail.count(" / ") < mcp._STDERR_QUOTE_LINES
        assert len(tail) < mcp._STDERR_QUOTE_CHARS + 60
        # the newest lines are what survive, not the oldest
        kept = list(server._stderr_lines)
        assert kept[-1].startswith("log line 0499")
        assert not any(ln.startswith("log line 0000") for ln in kept)
    finally:
        server.close()


def test_a_server_that_floods_stderr_still_completes_the_handshake():
    """Why the capture must be a DRAIN THREAD, not a read at failure time:
    an undrained pipe fills its OS buffer (~64KB) and blocks the child on
    its next write. This fixture writes ~100KB to stderr BEFORE serving, so
    it can only finish the handshake if someone is emptying the pipe."""
    server = mcp.MCPServer("flood", sys.executable, [_NOISY_SERVER, "--flood"])
    try:
        assert [t["name"] for t in server.tools] == ["echo"]
        assert server.call_tool("echo", {"text": "hi"}) == "echo: hi"
    finally:
        server.close()


def test_closing_a_server_leaves_no_stderr_thread_behind():
    server = mcp.MCPServer("fake", sys.executable, [_FAKE_SERVER])
    thread = server._stderr_thread
    server.close()
    assert not thread.is_alive()


def test_wipe_knows_about_mcp_server_keyring_credentials(tmp_path,
                                                         monkeypatch):
    """R150a: `mcp._resolve_env` resolves each `mcp_servers[].env` VALUE
    through keystore.get_key(), so an MCP server's GITHUB_TOKEN lives in the
    same "aurora-agent" keyring service as any provider key. But
    `_known_key_names()` — what `wipe` / `key clear --all` iterate — only
    collected providers' api_key_env, so `wipe` deleted AURORA_HOME while
    those credentials survived in the keyring. That is the exact
    "silently un-logging-out the user" hole ARCHITECTURE.md 5 says this
    function exists to close."""
    from aurora import __main__ as m

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "providers:\n"
        "  openrouter:\n"
        "    api_key_env: OPENROUTER_API_KEY\n"
        "mcp_servers:\n"
        "- name: github\n"
        "  command: srv\n"
        "  env:\n"
        "    GITHUB_PERSONAL_ACCESS_TOKEN: MY_GH_TOKEN\n"
        "- name: other\n"
        "  command: srv2\n"
        "  env:\n"
        "    API: OTHER_SECRET\n")
    monkeypatch.setattr(m, "_load_raw_config",
                        lambda: __import__("yaml").safe_load(cfg.read_text()))

    names = m._known_key_names()
    assert "OPENROUTER_API_KEY" in names
    # the env VALUES name keystore entries; the keys are the child's var names
    assert "MY_GH_TOKEN" in names and "OTHER_SECRET" in names
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in names


def test_mcp_server_raises_on_a_json_rpc_error():
    # a JSON-RPC-level error (unknown tool, -32601) is a transport-level
    # failure, distinct from an application-level tool error (isError:
    # true inside a successful response, covered by the "echo" tool below
    # via call_tool's own text join) — MCPServerError propagates so
    # tools.run_tool's existing generic exception handler (R42: "a raising
    # tool must not kill the turn") is what turns it into a result string,
    # exactly as it does for every other tool.
    server = mcp.MCPServer("fake", sys.executable, [_FAKE_SERVER])
    try:
        with pytest.raises(mcp.MCPServerError):
            server.call_tool("nonexistent", {})
    finally:
        server.close()


def test_run_tool_converts_an_mcp_error_into_a_result_string():
    manager = mcp.MCPManager([{"name": "fake", "command": sys.executable,
                              "args": [_FAKE_SERVER]}])
    tools.set_extensions(manager.specs(), manager.runners())
    manager.close_all()   # server process is gone — the next call must fail
    try:
        out = tools.run_tool("mcp_fake_echo", {"text": "hi"})
    finally:
        tools.set_extensions([], {})
    assert "tool error" in out


def test_mcp_manager_specs_are_prefixed_and_namespaced():
    manager = mcp.MCPManager([{"name": "fake", "command": sys.executable,
                              "args": [_FAKE_SERVER]}])
    try:
        specs = manager.specs()
        assert len(specs) == 1
        assert specs[0]["name"] == "mcp_fake_echo"
        assert "fake" in specs[0]["description"]
    finally:
        manager.close_all()


def test_mcp_manager_runner_calls_through_to_the_server():
    manager = mcp.MCPManager([{"name": "fake", "command": sys.executable,
                              "args": [_FAKE_SERVER]}])
    try:
        runners = manager.runners()
        assert runners["mcp_fake_echo"](text="hello") == "echo: hello"
    finally:
        manager.close_all()


def test_mcp_manager_survives_one_bad_server(tmp_path):
    # a server that isn't a real executable must not take down the others,
    # or Aurora itself — same "a tool must not kill the turn" principle
    # (R42), applied here at startup instead of per-call
    manager = mcp.MCPManager([
        {"name": "broken", "command": str(tmp_path / "does-not-exist")},
        {"name": "fake", "command": sys.executable, "args": [_FAKE_SERVER]},
    ])
    try:
        assert any("broken" in e for e in manager.errors)
        assert len(manager.specs()) == 1   # the working server still loaded
    finally:
        manager.close_all()


def test_mcp_manager_skips_entries_missing_name_or_command():
    manager = mcp.MCPManager([{"command": sys.executable}, {"name": "x"}])
    assert manager.specs() == []
    assert len(manager.errors) == 2


# ── R187a: a malformed tools/list must not cost the OTHER servers ──────────
def test_malformed_tool_entries_are_dropped_not_raised():
    # tools/list is the SERVER's data; it used to be stored verbatim, so
    # _to_aurora_spec's mcp_tool['name'] raised KeyError on a nameless entry.
    server = mcp.MCPServer("malformed", sys.executable, [_MALFORMED_SERVER])
    try:
        assert [t["name"] for t in server.tools] == ["good"]
        # one warning per dropped entry: no name, non-string name, blank
        # name, and a bare string instead of an object
        assert len(server.warnings) == 4
        assert server.call_tool("good", {}) == "ok"
    finally:
        server.close()


def test_one_malformed_server_does_not_lose_a_healthy_servers_tools():
    # the regression this fix exists for: the KeyError surfaced in
    # specs()/runners(), which run in mcp_extension.register() — AFTER
    # MCPManager.__init__'s per-server guard — so ALL mcp tools vanished and
    # the warning named no server at all.
    manager = mcp.MCPManager([
        {"name": "fake", "command": sys.executable, "args": [_FAKE_SERVER]},
        {"name": "bad", "command": sys.executable, "args": [_MALFORMED_SERVER]},
    ])
    try:
        names = sorted(s["name"] for s in manager.specs())
        assert names == ["mcp_bad_good", "mcp_fake_echo"]
        assert sorted(manager.runners()) == names
        # and the dropped entries are attributed to the server that sent them
        assert manager.errors, "malformed entries must not be silent"
        assert all(e.startswith("bad: ") for e in manager.errors)
    finally:
        manager.close_all()


def test_tools_list_that_isnt_a_list_fails_that_server_only():
    manager = mcp.MCPManager([
        {"name": "fake", "command": sys.executable, "args": [_FAKE_SERVER]},
        {"name": "wrongshape", "command": sys.executable,
         "args": [_MALFORMED_SERVER, "--not-a-list"]},
    ])
    try:
        assert [s["name"] for s in manager.specs()] == ["mcp_fake_echo"]
        assert any("wrongshape" in e and "expected a list" in e
                  for e in manager.errors)
    finally:
        manager.close_all()


# ── R187e: lifecycle + protocol hardening ──────────────────────────────────
def test_an_endlessly_chattering_server_still_hits_an_absolute_ceiling():
    # the per-read reset (R126, deliberate) means "data still arriving" keeps
    # extending the deadline — a server that never sends the reply blocked the
    # turn thread forever
    server = mcp.MCPServer("babble", sys.executable, [_BABBLING_SERVER],
                          timeout=0.4)
    started = time.monotonic()
    try:
        with pytest.raises(mcp.MCPServerError) as excinfo:
            server.call_tool("babble", {})
        assert "no reply to this request" in str(excinfo.value)
        elapsed = time.monotonic() - started
        # bounded by timeout * _MAX_TOTAL_WAIT_MULT, not unbounded, and not
        # cut short at the per-read timeout either (data really is arriving)
        assert elapsed > 0.4
        assert elapsed < 0.4 * mcp._MAX_TOTAL_WAIT_MULT + 3
    finally:
        server.close()


def test_a_server_sending_an_endless_line_hits_a_byte_ceiling(monkeypatch):
    """R197: R187e's ceilings all bound how LONG a server may take; none
    bounded how MUCH it may send before completing a single line.
    `_read_response` accumulates 64KB chunks until it finds a newline, so a
    server streaming an unterminated line grows the buffer at pipe speed for
    the whole `timeout * _MAX_TOTAL_WAIT_MULT` window. One JSON-RPC line is
    how a large tool result legitimately arrives, so this is the degenerate
    end of normal behaviour rather than a hostile special case.

    The cap is lowered here so the test costs megabytes, not the real 64MB.
    Fails without the fix: the buffer grows until the TIME ceiling trips,
    reporting a timeout rather than the real cause."""
    monkeypatch.setattr(mcp, "_MAX_RESPONSE_BYTES", 4 * 1024 * 1024)
    server = mcp.MCPServer("firehose", sys.executable, [_FIREHOSE_SERVER],
                           timeout=10)
    try:
        with pytest.raises(mcp.MCPServerError) as excinfo:
            server.call_tool("flood", {})
        assert "without a complete line" in str(excinfo.value)
        assert len(server._buf) < 8 * 1024 * 1024, "buffer outgrew the cap"
    finally:
        server.close()


def test_close_reaps_the_child_and_closes_its_pipes():
    server = mcp.MCPServer("fake", sys.executable, [_FAKE_SERVER])
    proc = server._proc
    server.close()
    # reaped, not left a zombie: poll() returns a status, not None
    assert proc.poll() is not None
    assert proc.stdin is None or proc.stdin.closed
    assert proc.stdout is None or proc.stdout.closed


def test_a_killed_child_also_has_its_pipes_closed():
    server = mcp.MCPServer("fake", sys.executable, [_FAKE_SERVER])
    proc = server._proc
    server._kill_unresponsive()
    assert proc.poll() is not None
    assert proc.stdin is None or proc.stdin.closed
    assert proc.stdout is None or proc.stdout.closed
    server.close()      # idempotent


def test_a_protocol_version_mismatch_is_recorded_but_not_fatal():
    # the handshake result was discarded, so a server answering with another
    # version looked identical to one that agreed
    server = mcp.MCPServer("oldver", sys.executable,
                          [_BABBLING_SERVER, "--version", "2000-01-01"])
    try:
        assert any("2000-01-01" in w and "2024-11-05" in w
                  for w in server.warnings)
        # recorded, NOT enforced — the server still works
        assert [t["name"] for t in server.tools] == ["babble"]
    finally:
        server.close()


def test_a_matching_protocol_version_produces_no_warning():
    server = mcp.MCPServer("fake", sys.executable, [_FAKE_SERVER])
    try:
        assert server.warnings == []
    finally:
        server.close()


# ── R187c: `timeout:` must reach MCPServer from config ─────────────────────
def test_config_timeout_is_forwarded_to_the_server():
    # supported by MCPServer from the start but never forwarded, so the 15s
    # default was unreachable — and crossing it kills the child permanently
    manager = mcp.MCPManager([{"name": "fake", "command": sys.executable,
                              "args": [_FAKE_SERVER], "timeout": 42}])
    try:
        assert manager._servers["fake"].timeout == 42.0
    finally:
        manager.close_all()


def test_timeout_defaults_when_not_configured():
    manager = mcp.MCPManager([{"name": "fake", "command": sys.executable,
                              "args": [_FAKE_SERVER]}])
    try:
        assert manager._servers["fake"].timeout == mcp._DEFAULT_TIMEOUT
    finally:
        manager.close_all()


@pytest.mark.parametrize("bad", ["soon", -1, 0, [], {}])
def test_an_invalid_timeout_warns_and_falls_back_to_the_default(bad):
    # a typo in config.yaml must not silently produce a 0s (or negative)
    # ceiling that kills every server on its first call
    manager = mcp.MCPManager([{"name": "fake", "command": sys.executable,
                              "args": [_FAKE_SERVER], "timeout": bad}])
    try:
        assert manager._servers["fake"].timeout == mcp._DEFAULT_TIMEOUT
        assert any("invalid timeout" in e for e in manager.errors)
    finally:
        manager.close_all()


# ── R187b: a duplicate mcp_servers name must not orphan a child ───────────
def test_duplicate_server_name_is_refused_before_spawning():
    before = _child_python_count()
    manager = mcp.MCPManager([
        {"name": "dup", "command": sys.executable, "args": [_FAKE_SERVER]},
        {"name": "dup", "command": sys.executable, "args": [_FAKE_SERVER]},
    ])
    try:
        # only one server, and only one set of tools (two would have produced
        # colliding mcp_dup_echo specs anyway)
        assert [s["name"] for s in manager.specs()] == ["mcp_dup_echo"]
        assert any("duplicate" in e for e in manager.errors)
        # the refused entry must never have been started
        assert _child_python_count() == before + 1
    finally:
        manager.close_all()
    time.sleep(0.5)
    # and nothing is left behind afterwards — previously the displaced child
    # was unreachable from close_all()/atexit and survived the whole session
    assert _child_python_count() <= before


def test_a_malformed_server_leaves_no_orphaned_child(tmp_path):
    # a tools/list breach raises from __init__, which must still not leak the
    # child it spawned (the R145a guarantee, re-checked for this new path)
    before = _child_python_count()
    manager = mcp.MCPManager([
        {"name": "wrongshape", "command": sys.executable,
         "args": [_MALFORMED_SERVER, "--not-a-list"]}])
    manager.close_all()
    time.sleep(0.5)
    assert _child_python_count() <= before


def test_mcp_read_timeout_fires_against_a_server_that_never_replies():
    # bug found in review: readline() is a BLOCKING call — a
    # `while time.monotonic() < deadline` loop around it only re-checks the
    # deadline BETWEEN lines, so it enforced the timeout against a SLOW
    # server but not a SILENT one (accepts the request, produces no output
    # at all — a real hang, e.g. a wedged/deadlocked server process). Since
    # MCPServer() runs during Engine construction, this used to hang
    # Aurora's entire startup with no way out. select.select fixes it by
    # enforcing the deadline on the wait itself.
    server = mcp.MCPServer("hanging", sys.executable, [_HANGING_SERVER],
                           timeout=1.0)
    try:
        import time
        start = time.monotonic()
        with pytest.raises(mcp.MCPServerError, match="timed out"):
            server.call_tool("hang", {})
        elapsed = time.monotonic() - start
        # bounded well under a hypothetical "hang forever" — generous
        # margin over the 1s timeout for slow CI, nowhere near infinite
        assert elapsed < 5.0
    finally:
        server.close()


# ── R126: select() polls the fd, a buffered line is invisible to it ────────
def test_mcp_handles_a_notification_sent_in_the_same_burst_as_the_reply():
    """R126. The regression that R125's own fix introduced.

    `select` reports readability of the file DESCRIPTOR; the old text-mode
    `readline()` read from a TextIOWrapper's internal buffer. A server that
    emits a notification and its reply in one flush (ordinary MCP — real
    servers log progress this way) puts both lines into that buffer on a
    single OS read. The old client returned the notification, selected
    again, saw an idle fd, and timed out — then killed a healthy server
    whose reply it was already holding in memory.

    Handshake included: `initialize` and `tools/list` go through the same
    path, so constructing the server at all is part of what this asserts —
    on the old code this raised before the body ever ran."""
    import time
    start = time.monotonic()
    server = mcp.MCPServer("chatty", sys.executable, [_CHATTY_SERVER],
                           timeout=2.0)
    try:
        assert [t["name"] for t in server.tools] == ["echo"]
        assert server.call_tool("echo", {"text": "hi"}) == "echo: hi"
        # three round trips (initialize, tools/list, tools/call). Each one
        # burned the full 2s timeout before this fix; well under that now.
        assert time.monotonic() - start < 2.0
    finally:
        server.close()


def test_mcp_timeout_is_enforced_when_a_server_stalls_MID_LINE():
    """R126, the other half of the same mismatch. A server that writes half a
    line and then stalls left the old client blocked INSIDE `readline()`,
    where the surrounding `while time.monotonic() < deadline` loop could not
    re-check anything — so `self.timeout` was unenforceable mid-line, the
    same "silent server hangs startup" failure R125 set out to fix, just
    reached one byte later.

    The discriminating assertion is the CLOCK, not the exception: both the
    old and new code eventually raise here, but the old one only does so
    after the server finally finishes the line. With a 1s timeout against a
    6s mid-line stall, the old code takes ~6s and the fixed code ~1s."""
    import time
    prog = textwrap.dedent("""
        import sys, time, json
        for line in sys.stdin:
            msg = json.loads(line)
            mid, method = msg.get("id"), msg.get("method")
            if method == "notifications/initialized":
                continue
            if method == "initialize":
                body = {"protocolVersion": "2024-11-05", "capabilities": {},
                        "serverInfo": {"name": "split", "version": "0"}}
            elif method == "tools/list":
                body = {"tools": [{"name": "echo", "description": "d",
                                   "inputSchema": {"type": "object",
                                                   "properties": {}}}]}
            else:
                body = {"content": [{"type": "text", "text": "ok"}],
                        "isError": False}
            out = json.dumps({"jsonrpc": "2.0", "id": mid, "result": body})
            if method == "tools/call":
                sys.stdout.write(out[:len(out) // 2]); sys.stdout.flush()
                time.sleep(6)                       # stall MID-LINE
            sys.stdout.write(out + "\\n"); sys.stdout.flush()
    """)
    server = mcp.MCPServer("split", sys.executable, ["-c", prog], timeout=1.0)
    try:
        start = time.monotonic()
        with pytest.raises(mcp.MCPServerError, match="timed out"):
            server.call_tool("echo", {})
        elapsed = time.monotonic() - start
        # the 1s deadline is honoured against the stall, not deferred until
        # the server's 6s sleep ends (generous upper bound for slow CI)
        assert elapsed < 4.0, f"timeout not enforced mid-line ({elapsed:.1f}s)"
    finally:
        server.close()


def test_mcp_deadline_resets_while_a_slow_server_keeps_streaming_progress():
    """Review pass: the deadline in `_read_response` was computed once at the
    top and never touched again, so a server that is genuinely ALIVE and
    doing real work — streaming `notifications/progress` lines while a slow
    tool call runs — got killed for taking longer than `self.timeout` even
    though data kept arriving the whole time. R126's own stated rationale
    ("don't kill a healthy server") argues for judging liveness by "is data
    still arriving," not a fixed wall-clock line computed before the call
    even started.

    The server below sends 4 progress notifications 0.3s apart (1.2s total)
    before the real reply, against a 0.5s timeout — the OLD deadline
    (now + 0.5s, set once) would have expired long before the reply, despite
    four chunks of genuine data arriving in between."""
    prog = textwrap.dedent("""
        import sys, time, json
        for line in sys.stdin:
            msg = json.loads(line)
            mid, method = msg.get("id"), msg.get("method")
            if method == "notifications/initialized":
                continue
            if method == "initialize":
                body = {"protocolVersion": "2024-11-05", "capabilities": {},
                        "serverInfo": {"name": "slow", "version": "0"}}
            elif method == "tools/list":
                body = {"tools": [{"name": "slow", "description": "d",
                                   "inputSchema": {"type": "object",
                                                   "properties": {}}}]}
            else:
                for _ in range(4):
                    time.sleep(0.3)
                    note = {"jsonrpc": "2.0", "method": "notifications/progress",
                            "params": {}}
                    sys.stdout.write(json.dumps(note) + "\\n")
                    sys.stdout.flush()
                body = {"content": [{"type": "text", "text": "done"}],
                        "isError": False}
            out = json.dumps({"jsonrpc": "2.0", "id": mid, "result": body})
            sys.stdout.write(out + "\\n")
            sys.stdout.flush()
    """)
    server = mcp.MCPServer("slow", sys.executable, ["-c", prog], timeout=0.5)
    try:
        assert server.call_tool("slow", {}) == "done"
    finally:
        server.close()


# ── tools.needs_approval: every mcp_* call is gated, unconditionally ───────
def test_mcp_tools_always_need_approval():
    assert tools.needs_approval("mcp_github_create_issue")
    assert tools.needs_approval("mcp_anything_at_all")


def test_non_mcp_tools_unaffected_by_the_prefix_check():
    assert tools.needs_approval("write_file")
    assert not tools.needs_approval("read_file")


# ── aurora.extensions: file discovery + the two registration styles ───────
def test_discover_loads_a_static_spec_runners_extension(tmp_path, monkeypatch):
    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir()
    (ext_dir / "greet.py").write_text(textwrap.dedent("""
        SPEC = [{"name": "greet", "description": "says hi",
                "parameters": {"type": "object", "properties": {}}}]
        RUNNERS = {"greet": lambda **_: "hi"}
    """))
    monkeypatch.setattr(extensions, "_dirs", lambda: [ext_dir])
    specs, runners, warnings = extensions.discover()
    assert not warnings
    assert specs[0]["name"] == "greet"
    assert runners["greet"]() == "hi"


def test_discover_calls_register_with_the_engine_when_given(tmp_path, monkeypatch):
    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir()
    (ext_dir / "dynamic.py").write_text(textwrap.dedent("""
        def register(engine):
            return ([{"name": "cfg_tool", "description": "d",
                      "parameters": {"type": "object", "properties": {}}}],
                    {"cfg_tool": lambda **_: engine.marker})
    """))
    monkeypatch.setattr(extensions, "_dirs", lambda: [ext_dir])

    class FakeEngine:
        marker = "engine-was-here"

    specs, runners, warnings = extensions.discover(FakeEngine())
    assert not warnings
    assert runners["cfg_tool"]() == "engine-was-here"


def test_discover_skips_register_without_an_engine(tmp_path, monkeypatch):
    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir()
    (ext_dir / "dynamic.py").write_text(
        "def register(engine):\n    return [], {}\n")
    monkeypatch.setattr(extensions, "_dirs", lambda: [ext_dir])
    specs, runners, warnings = extensions.discover(engine=None)
    assert specs == [] and runners == {} and warnings == []


def test_a_broken_extension_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir()
    (ext_dir / "broken.py").write_text("this is not valid python (((\n")
    (ext_dir / "fine.py").write_text(
        'SPEC = [{"name": "ok", "description": "d", '
        '"parameters": {"type": "object", "properties": {}}}]\n'
        'RUNNERS = {"ok": lambda **_: "ok"}\n')
    monkeypatch.setattr(extensions, "_dirs", lambda: [ext_dir])
    specs, runners, warnings = extensions.discover()
    assert any("broken.py" in w for w in warnings)
    assert specs and specs[0]["name"] == "ok"   # the good one still loaded


def test_a_register_that_raises_is_skipped_not_fatal(tmp_path, monkeypatch):
    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir()
    (ext_dir / "raises.py").write_text(
        "def register(engine):\n    raise RuntimeError('boom')\n")
    monkeypatch.setattr(extensions, "_dirs", lambda: [ext_dir])
    specs, runners, warnings = extensions.discover(engine=object())
    assert any("raises.py" in w and "boom" in w for w in warnings)


# ── End-to-end: Engine wiring picks up a configured MCP server ────────────
_CFG = """\
providers:
  local: {{type: openai, base_url: http://127.0.0.1:1, api_key_env: X}}
models:
  - {{provider: local, model: m}}
mcp_servers:
  - name: fake
    command: {command}
    args: ["{arg}"]
"""


def test_engine_wires_mcp_tools_into_tools_specs(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG.format(command=sys.executable, arg=_FAKE_SERVER))
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        assert not e.extension_warnings
        assert e._mcp_manager is not None
        names = [s["name"] for s in tools.specs()]
        assert "mcp_fake_echo" in names
        assert tools.run_tool("mcp_fake_echo", {"text": "yo"}) == "echo: yo"
    finally:
        if e._mcp_manager:
            e._mcp_manager.close_all()
        tools.set_extensions([], {})   # don't leak into later tests


def test_engine_without_mcp_servers_has_no_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  local: {type: openai, base_url: x}\n"
                  "models:\n  - {provider: local, model: m}\n")
    from aurora.engine import Engine
    e = Engine(str(cfg))
    assert e._mcp_manager is None
    assert e.extension_warnings == []


# ── R122: /extensions command + startup-banner visibility ─────────────────
def test_extension_tool_specs_excludes_builtins_and_web_and_context():
    from aurora import ui
    tools.set_extensions([], {})
    try:
        names = {s["name"] for s in ui._extension_tool_specs()}
        assert names == set()
    finally:
        tools.set_extensions([], {})


def test_extension_tool_specs_includes_loaded_extension_tools(tmp_path, monkeypatch):
    from aurora import ui
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG.format(command=sys.executable, arg=_FAKE_SERVER))
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        names = {s["name"] for s in ui._extension_tool_specs()}
        assert "mcp_fake_echo" in names
        assert "lint_check" in names   # bundled alongside the configured MCP server
    finally:
        if e._mcp_manager:
            e._mcp_manager.close_all()
        tools.set_extensions([], {})


# ── R187d: a malformed extension must not stop Aurora starting ─────────────
def test_set_extensions_skips_a_spec_that_isnt_an_object():
    try:
        warnings = tools.set_extensions(["oops", {"name": "fine"}],
                                       {"fine": lambda **_: "ok"})
        assert [s["name"] for s in tools._EXTENSION_SPECS] == ["fine"]
        assert any("isn't an object" in w for w in warnings)
    finally:
        tools.set_extensions([], {})


def test_set_extensions_skips_a_nameless_spec():
    # a nameless spec used to be kept, reaching the model as an unnamed tool
    # with no runner behind it — advertised and permanently uncallable
    try:
        warnings = tools.set_extensions(
            [{"description": "no name"}, {"name": "  "}], {})
        assert tools._EXTENSION_SPECS == []
        assert len(warnings) == 2
        assert all("no usable name" in w for w in warnings)
    finally:
        tools.set_extensions([], {})


def test_a_broken_extension_file_does_not_prevent_engine_construction(
        tmp_path, monkeypatch):
    # `SPEC = ["oops"]` used to raise AttributeError out of Engine.__init__
    # (set_extensions ran unguarded there), so one bad file in
    # ~/.aurora/extensions/ meant Aurora would not start at all
    home = tmp_path / "home"
    ext = home / "extensions"
    ext.mkdir(parents=True)
    (ext / "broken.py").write_text('SPEC = ["oops"]\nRUNNERS = {}\n')
    monkeypatch.setenv("AURORA_HOME", str(home))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG.format(command=sys.executable, arg=_FAKE_SERVER))
    from aurora.engine import Engine
    e = Engine(str(cfg))            # must not raise
    try:
        assert any("broken" in w or "isn't an object" in w
                  for w in e.extension_warnings)
        # the healthy bundled tools survived
        assert "lint_check" in {s["name"] for s in tools._EXTENSION_SPECS}
    finally:
        if e._mcp_manager:
            e._mcp_manager.close_all()
        tools.set_extensions([], {})


def test_extensions_command_lists_tools_and_how_to_add_one(tmp_path, monkeypatch, capsys):
    from aurora import ui
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG.format(command=sys.executable, arg=_FAKE_SERVER))
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        ui._handle_command(e, None, "/extensions")
        out = capsys.readouterr().out
        assert "mcp_fake_echo" in out
        assert "lint_check" in out
        assert "EXTENSIONS.md" in out
        assert str(tmp_path / "home" / "extensions") in out
    finally:
        if e._mcp_manager:
            e._mcp_manager.close_all()
        tools.set_extensions([], {})


def test_extensions_command_reports_none_loaded(tmp_path, monkeypatch, capsys):
    # lint_check is bundled unconditionally, so "nothing loaded" only
    # happens if even the bundled dir is empty — simulate that directly
    # rather than pretending a normal config ever has zero extensions.
    from aurora import extensions, ui
    monkeypatch.setattr(extensions, "_dirs", list)
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  local: {type: openai, base_url: x}\n"
                  "models:\n  - {provider: local, model: m}\n")
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        ui._handle_command(e, None, "/extensions")
        out = capsys.readouterr().out
        assert "no extensions loaded" in out
    finally:
        tools.set_extensions([], {})


def test_lint_check_always_bundled_by_default(tmp_path, monkeypatch, capsys):
    from aurora import ui
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  local: {type: openai, base_url: x}\n"
                  "models:\n  - {provider: local, model: m}\n")
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        ui._handle_command(e, None, "/extensions")
        out = capsys.readouterr().out
        assert "lint_check" in out
        # R136 added a second always-bundled extension (refresh_model_prices);
        # R157 moved web_search + web_fetch out of the engine into
        # extensions_bundled/web_extension.py, so they are bundled tools now too
        assert "4 extension tools loaded" in out
        assert "web_search" in out and "web_fetch" in out
    finally:
        tools.set_extensions([], {})


# ── review pass: stdin is a raw unbuffered stream, write() can be short ────
def test_write_all_retries_a_short_write_until_everything_is_sent():
    """R126 switched `stdin` to a RAW unbuffered pipe (`bufsize=0`), whose
    `write()` is explicitly allowed to send fewer bytes than given — unlike
    a `BufferedWriter`, which loops internally on flush(). Without its own
    retry loop, `_send` would silently drop the unsent tail of any payload
    bigger than one short write's worth (a long patch, a big JSON tool
    argument), and the child would see truncated, malformed JSON."""

    class _ShortWriteStdin:
        def __init__(self):
            self.received = bytearray()

        def write(self, data):
            n = min(3, len(data))          # simulate a short write
            self.received.extend(bytes(data[:n]))
            return n

        def flush(self):
            pass

    server = mcp.MCPServer.__new__(mcp.MCPServer)
    fake_stdin = _ShortWriteStdin()
    server._proc = type("P", (), {"stdin": fake_stdin})()

    payload = b'{"jsonrpc": "2.0", "method": "tools/call", "id": 1}'
    server._write_all(payload)
    assert bytes(fake_stdin.received) == payload


# ── aurora.extensions: scaffolding (R160) ──────────────────────────────────
def test_scaffold_writes_a_loadable_spec_runners_file(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "aurora_home", lambda: tmp_path)
    path = extensions.scaffold("my tool")
    assert path == tmp_path / "extensions" / "my_tool.py"
    assert path.exists()

    monkeypatch.setattr(extensions, "_dirs", lambda: [path.parent])
    specs, runners, warnings = extensions.discover()
    assert not warnings
    assert specs[0]["name"] == "my_tool"
    # R171/I5: a scaffolded-but-unedited tool must fail loudly, not return a
    # success-shaped "not implemented" string that lies to the model
    with pytest.raises(NotImplementedError):
        runners["my_tool"]()


def test_scaffold_refuses_to_overwrite_an_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "aurora_home", lambda: tmp_path)
    extensions.scaffold("dup")
    with pytest.raises(FileExistsError):
        extensions.scaffold("dup")


def test_scaffold_rejects_a_name_that_slugs_to_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "aurora_home", lambda: tmp_path)
    with pytest.raises(ValueError):
        extensions.scaffold("***")


def test_extensions_new_command_scaffolds_and_reports_the_path(
        tmp_path, monkeypatch, capsys):
    from aurora import ui
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG.format(command=sys.executable, arg=_FAKE_SERVER))
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        ui._handle_command(e, None, "/extensions new my_tool")
        out = capsys.readouterr().out
        assert str(tmp_path / "home" / "extensions" / "my_tool.py") in out
        assert (tmp_path / "home" / "extensions" / "my_tool.py").exists()
    finally:
        if e._mcp_manager:
            e._mcp_manager.close_all()
        tools.set_extensions([], {})


def test_fallback_command_toggles_and_persists(tmp_path, monkeypatch, capsys):
    from aurora import ui
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG.format(command=sys.executable, arg=_FAKE_SERVER))
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        assert e.model_fallback is False
        ui._handle_command(e, None, "/fallback on")
        out = capsys.readouterr().out
        assert "ON" in out
        assert e.model_fallback is True
        e2 = Engine(str(cfg))
        assert e2.model_fallback is True   # persisted
    finally:
        if e._mcp_manager:
            e._mcp_manager.close_all()
        tools.set_extensions([], {})


def test_search_command_requires_text(tmp_path, monkeypatch, capsys):
    from aurora import ui
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG.format(command=sys.executable, arg=_FAKE_SERVER))
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        ui._handle_command(e, None, "/search")
        out = capsys.readouterr().out
        assert "usage" in out
    finally:
        if e._mcp_manager:
            e._mcp_manager.close_all()
        tools.set_extensions([], {})


def test_extensions_new_command_requires_a_name(tmp_path, monkeypatch, capsys):
    from aurora import ui
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG.format(command=sys.executable, arg=_FAKE_SERVER))
    from aurora.engine import Engine
    e = Engine(str(cfg))
    try:
        ui._handle_command(e, None, "/extensions new")
        out = capsys.readouterr().out
        assert "usage" in out
    finally:
        if e._mcp_manager:
            e._mcp_manager.close_all()
        tools.set_extensions([], {})
