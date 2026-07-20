"""Core tests — no network; providers are faked. Run: python -m pytest tests/"""

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("AURORA_HOME", tempfile.mkdtemp())

from aurora import agent, approve, tools, compact, secrets  # noqa: E402
from aurora.providers.base import ToolCall, TurnResult  # noqa: E402


# ── tools ─────────────────────────────────────────────────────────────────
def test_read_write_edit(tmp_path):
    f = tmp_path / "x.txt"
    assert "wrote" in tools.write_file(str(f), "hello\nworld\n")
    assert tools.read_file(str(f)).startswith("hello")
    assert "edited" in tools.edit_file(str(f), "world", "there")
    assert "there" in tools.read_file(str(f))


def test_edit_rejects_nonunique(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("aa aa")
    assert "appears 2 times" in tools.edit_file(str(f), "aa", "b")


def test_edit_missing_anchor(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("abc")
    assert "not found" in tools.edit_file(str(f), "zzz", "b")


# ── allowlist ──────────────────────────────────────────────────────────────
def test_allowlist_command_prefix():
    approve.save({"run_command": ["git status"], "write_file": [], "edit_file": []})
    assert approve.is_allowed("run_command", {"command": "git status --short"})
    assert not approve.is_allowed("run_command", {"command": "rm -rf /"})


def test_allowlist_single_token_is_exact_match_only():
    # pre-R43 rules like "rm" must never prefix-approve "rm -rf /" —
    # they survive only as an exact match on the bare command (not a
    # SAFE_COMMANDS entry, so no args-agnostic generalization either)
    approve.save({"run_command": ["rm", "xcodebuild"], "write_file": [], "edit_file": []})
    assert not approve.is_allowed("run_command", {"command": "rm -rf /"})
    assert not approve.is_allowed("run_command", {"command": "xcodebuild -scheme Foo"})
    assert approve.is_allowed("run_command", {"command": "rm"})
    assert approve.legacy_rules() == ["rm", "xcodebuild"]


def test_allowlist_safe_command_generalizes_across_args():
    # a SAFE_COMMANDS single-token rule (read-only, no destructive/exec
    # risk) prefix-matches regardless of args — "always allow" on `find
    # /path/A` in one session must also cover `find /path/B` in another,
    # instead of re-prompting per path (R: cross-session allowlist UX)
    approve.save({"run_command": ["find"], "write_file": [], "edit_file": []})
    assert approve.is_allowed("run_command", {"command": "find /path/A -name '*.py'"})
    assert approve.is_allowed("run_command", {"command": "find /totally/different/path"})
    assert approve.legacy_rules() == []  # not surfaced as a stale legacy rule


def test_add_rule_stores_bare_name_for_safe_commands():
    rule = approve.add_rule("run_command", {"command": "find /path/A -name '*.py'"})
    assert rule == "find"
    assert approve.is_allowed("run_command", {"command": "find /path/B"})


def test_allowlist_path_glob():
    approve.save({"run_command": [], "write_file": ["/tmp/ok/*"], "edit_file": []})
    assert approve.is_allowed("write_file", {"path": "/tmp/ok/a.txt"})
    assert not approve.is_allowed("write_file", {"path": "/tmp/no/a.txt"})


def test_add_rule_stores_command_prefix():
    approve.save({"run_command": [], "write_file": [], "edit_file": []})
    approve.add_rule("run_command", {"command": "pytest tests/ -x"})
    # first TWO tokens: "pytest" alone would auto-approve every future pytest
    assert "pytest tests/" in approve.load()["run_command"]
    assert approve.is_allowed("run_command", {"command": "pytest tests/ -q"})
    # token boundary: an allowlisted prefix must not match a longer word
    approve.add_rule("run_command", {"command": "git status"})
    assert not approve.is_allowed("run_command", {"command": "gitk"})


def test_allowlist_matches_across_path_spellings(tmp_path):
    # the real bug: 'always allow' for a `bash <script>` command must catch the
    # model's next run even if it spells the path differently (quotes / ~ / abs)
    approve.save({"run_command": [], "write_file": [], "edit_file": []})
    script = tmp_path / "build.sh"
    approve.add_rule("run_command", {"command": f'bash "{script}"'})   # quoted
    for spelling in (f'bash {script}',                 # unquoted absolute
                     f'bash "{script}"',               # quoted absolute
                     f'bash {script} --verbose'):      # + args (prefix)
        assert approve.is_allowed("run_command", {"command": spelling}), spelling
    # stored form is normalized (no quotes), so it doesn't pile up duplicates
    assert approve.load()["run_command"] == [f"bash {script}"]


def _mk_provider():
    from aurora.providers.openai_compat import OpenAICompatProvider
    prov = OpenAICompatProvider("openrouter",
                                {"base_url": "https://openrouter.ai/api/v1"}, 300)
    prov._client_for = lambda base: object()   # never build/use a real client
    return prov


def _sse_ok(content="hi"):
    chunk = (f'data: {{"choices":[{{"delta":{{"content":"{content}"}},'
             f'"finish_reason":"stop"}}]}}')
    return [("status", 200, None), ("line", chunk, None),
            ("line", "data: [DONE]", None)]


def test_turn_retries_transient_connection_reset(monkeypatch):
    import httpx
    from aurora.providers import openai_compat as oc
    calls = {"n": 0}
    def fake_sse(open_stream, cancel, poll=0.15):
        calls["n"] += 1
        if calls["n"] == 1:            # stale pooled connection resets on reuse
            raise httpx.RemoteProtocolError("Server disconnected")
        yield from _sse_ok("hi")
    monkeypatch.setattr(oc, "cancellable_sse", fake_sse)
    got = []
    res = _mk_provider().turn("m", [{"role": "user", "content": "x"}], "", None,
                              lambda t: got.append(t), lambda: False)
    assert calls["n"] == 2            # retried once, then succeeded
    assert res.text == "hi" and "".join(got) == "hi"


def test_turn_gives_up_after_retries(monkeypatch):
    import httpx
    from aurora.providers import openai_compat as oc
    from aurora.providers.base import ProviderError
    calls = {"n": 0}
    def always_reset(open_stream, cancel, poll=0.15):
        calls["n"] += 1
        raise httpx.RemoteProtocolError("Server disconnected")
        yield  # pragma: no cover — generator
    monkeypatch.setattr(oc, "cancellable_sse", always_reset)
    import pytest
    with pytest.raises(ProviderError):
        _mk_provider().turn("m", [{"role": "user", "content": "x"}], "", None,
                            lambda t: None, lambda: False)
    assert calls["n"] == 3           # 1 try + 2 retries, then raise


def test_turn_keeps_partial_on_midstream_drop(monkeypatch):
    import httpx
    from aurora.providers import openai_compat as oc
    calls = {"n": 0}
    def drop_midstream(open_stream, cancel, poll=0.15):
        calls["n"] += 1
        yield ("status", 200, None)
        yield ("line", 'data: {"choices":[{"delta":{"content":"partial"}}]}', None)
        raise httpx.ReadError("reset mid-stream")   # after text already streamed
    monkeypatch.setattr(oc, "cancellable_sse", drop_midstream)
    got = []
    res = _mk_provider().turn("m", [{"role": "user", "content": "x"}], "", None,
                              lambda t: got.append(t), lambda: False)
    assert calls["n"] == 1           # NOT retried — would duplicate output
    assert res.text.startswith("partial") and res.stop_reason == "interrupted"


def test_remote_provider_skips_props_probe():
    # /props is llama.cpp-only; probing it on a remote API wastes a ~6s request
    # on the UI thread at startup. A remote provider must return None WITHOUT
    # touching the network.
    from aurora.providers.openai_compat import OpenAICompatProvider
    prov = OpenAICompatProvider("openrouter",
                                {"base_url": "https://openrouter.ai/api/v1"}, 300)
    # if it tried to connect, accessing _client would build/use it — trip a guard
    prov.__dict__["_http"] = _Boom()
    assert prov.live_context_limit() is None


class _Boom:
    def get(self, *a, **k):
        raise AssertionError("remote provider must not probe /props")


def test_allowlist_tilde_matches_absolute():
    import os
    home = os.path.expanduser("~")
    approve.save({"run_command": [f"bash {home}/x.sh"],
                  "write_file": [], "edit_file": []})
    assert approve.is_allowed("run_command", {"command": "bash ~/x.sh"})


# ── keystore.clear_key (aurora key clear / aurora wipe) ────────────────────
def _fake_keyring_module(fake_store: dict):
    """clear_key/store_key call `import keyring` and its functions directly
    (not a keystore-level wrapper) — swap the whole module for an in-memory
    fake so tests never touch the real OS keychain."""
    import types
    m = types.ModuleType("keyring")
    m.get_password = lambda service, name: fake_store.get(name)
    m.set_password = lambda service, name, value: fake_store.__setitem__(name, value)
    def _delete(service, name):
        del fake_store[name]
    m.delete_password = _delete
    return m


def test_clear_key_removes_from_keyring(monkeypatch):
    from aurora import keystore
    import sys
    fake_store: dict = {}
    monkeypatch.setitem(sys.modules, "keyring", _fake_keyring_module(fake_store))

    keystore.store_key("TEST_VAR", "secret-value")
    assert fake_store.get("TEST_VAR") == "secret-value"

    removed = keystore.clear_key("TEST_VAR")
    assert "OS keyring" in removed
    assert "TEST_VAR" not in fake_store


def test_clear_key_on_nothing_stored_is_a_noop(monkeypatch):
    from aurora import keystore
    import sys
    monkeypatch.setitem(sys.modules, "keyring", _fake_keyring_module({}))
    assert keystore.clear_key("NEVER_STORED_VAR") == []


# ── flatten (cross-provider switch / compact) ──────────────────────────────
def test_flatten_mixed_history():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "reading"},
            {"type": "tool_use", "name": "read_file", "input": {"path": "x"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "content": "file body"}]},
        {"role": "assistant", "content": "done"},
    ]
    out = compact.flatten_history(msgs)
    assert "User: hi" in out and "read_file" in out and "file body" in out


# ── agent loop with a fake provider ────────────────────────────────────────
class FakeProvider:
    """Returns queued TurnResults; records messages it was given."""
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def turn(self, model, messages, system, tools_, on_text, cancel):
        on_text("chunk ")
        self.calls += 1
        return self.results.pop(0)

    def assistant_message(self, r):
        return {"role": "assistant", "content": r.text}

    def tool_result_message(self, call, output):
        return {"role": "tool", "tool_call_id": call.id, "content": output}


def _cb(approve_ans="y", cont=True, log=None, secret_ans=None):
    log = log if log is not None else []
    return agent.AgentCallbacks(
        on_text=lambda t: None,
        on_tool_start=lambda n, a: log.append(("start", n)),
        on_tool_result=lambda n, o: log.append(("result", n, o)),
        approve=lambda t, a, d: approve_ans,
        ask_continue=lambda i: cont,
        notify=lambda m: log.append(("notify", m)),
        cancelled=lambda: False,
        # R58: None (default) leaves the feature OFF, matching the engine's
        # "don't pass the callback when runtime.redact_secrets is false"
        secret_challenge=(lambda ctx, matches, **_k: secret_ans)
                        if secret_ans is not None else None,
    )


def test_agent_runs_tool_then_finishes(tmp_path):
    approve.save({"run_command": [], "write_file": ["*"], "edit_file": []})
    f = tmp_path / "o.txt"
    prov = FakeProvider([
        TurnResult(text="", tool_calls=[ToolCall("1", "write_file",
                   {"path": str(f), "content": "hi"})], stop_reason="tool_use"),
        TurnResult(text="all done", stop_reason="end"),
    ])
    msgs = [{"role": "user", "content": "make the file"}]
    t = agent.run_turn(prov, "m", msgs, "sys", _cb(), 5, True, False)
    assert f.read_text() == "hi"
    assert t.iterations == 2
    assert prov.calls == 2


# ── R58: secret detection in TOOL OUTPUT (covers read-only tools too) ──────
def _secret_read_turn(tmp_path, secret_ans):
    # read_file is NOT in NEEDS_APPROVAL — proves the scan fires even for a
    # tool that never goes through the approval gate at all
    f = tmp_path / "leaky.env"
    f.write_text("AWS_ACCESS_KEY_ID=AKIA" + "Q" * 16)
    prov = FakeProvider([
        TurnResult(text="", tool_calls=[ToolCall("1", "read_file",
                   {"path": str(f)})], stop_reason="tool_use"),
        TurnResult(text="all done", stop_reason="end"),
    ])
    log = []
    msgs = [{"role": "user", "content": "read the env file"}]
    t = agent.run_turn(prov, "m", msgs, "sys", _cb(log=log, secret_ans=secret_ans),
                       5, True, False)
    return t, log


def test_tool_output_keep_sends_secret_unchanged(tmp_path):
    t, log = _secret_read_turn(tmp_path, "keep")
    result_entries = [e for e in log if e[0] == "result"]
    assert any("AKIA" in e[2] for e in result_entries)
    assert t.iterations == 2   # turn proceeded normally


def test_tool_output_redact_replaces_secret(tmp_path):
    t, log = _secret_read_turn(tmp_path, "redact")
    result_entries = [e for e in log if e[0] == "result"]
    assert all("AKIA" not in e[2] for e in result_entries)
    assert any("<secret>" in e[2] for e in result_entries)
    assert t.iterations == 2


def test_tool_output_stop_halts_the_turn(tmp_path):
    t, log = _secret_read_turn(tmp_path, "stop")
    # the tool ran (output was produced), but never reached on_tool_result —
    # the challenge intercepts it before display/history, and no further
    # iteration (the "final answer" turn) ever happens
    assert not any(e[0] == "result" for e in log)
    assert any(e[0] == "notify" and "secret" in e[1] for e in log)
    assert t.iterations == 1


def test_tool_output_not_scanned_when_feature_off(tmp_path):
    # secret_ans=None -> _cb() passes secret_challenge=None, matching the
    # engine's behavior when runtime.redact_secrets is false: no scan at all
    t, log = _secret_read_turn(tmp_path, None)
    result_entries = [e for e in log if e[0] == "result"]
    assert any("AKIA" in e[2] for e in result_entries)   # unchanged, not stopped
    assert t.iterations == 2


# ── R58 extension: secret in a run_command PARAMETER — notice only ────────
def test_run_command_param_secret_is_notice_only_and_still_runs(tmp_path):
    approve.save({"run_command": ["*"], "write_file": [], "edit_file": []})
    marker = tmp_path / "ran.txt"
    secret = "AKIA" + "N0T1C3N0T1C3N0T1"
    cmd = f'echo hi {secret} > "{marker}"'
    prov = FakeProvider([
        TurnResult(text="", tool_calls=[ToolCall("1", "run_command",
                   {"command": cmd})], stop_reason="tool_use"),
        TurnResult(text="all done", stop_reason="end"),
    ])
    log = []
    msgs = [{"role": "user", "content": "run it"}]
    # secret_ans="keep": the command's OWN OUTPUT also happens to contain the
    # secret (it echoed it) — "keep" isolates the assertions below to the
    # NEW param-notice behavior, not the pre-existing output-challenge path
    t = agent.run_turn(prov, "m", msgs, "sys", _cb(log=log, secret_ans="keep"),
                       5, True, False)
    notices = [e[1] for e in log if e[0] == "notify"]
    assert any("possible secret in this command" in m for m in notices)
    # never blocked, never altered: the REAL command actually ran
    assert marker.read_text().strip() == f"hi {secret}"
    assert t.iterations == 2


def test_run_command_param_notice_skipped_when_feature_off(tmp_path):
    approve.save({"run_command": ["*"], "write_file": [], "edit_file": []})
    marker = tmp_path / "ran2.txt"
    secret = "AKIA" + "N0T1C3N0T1C3N0T1"
    cmd = f'echo hi > "{marker}"'   # secret only in the command, not the output
    prov = FakeProvider([
        TurnResult(text="", tool_calls=[ToolCall("1", "run_command",
                   {"command": f'{cmd} # {secret}'})], stop_reason="tool_use"),
        TurnResult(text="all done", stop_reason="end"),
    ])
    log = []
    msgs = [{"role": "user", "content": "run it"}]
    agent.run_turn(prov, "m", msgs, "sys", _cb(log=log, secret_ans=None),
                   5, True, False)   # secret_ans=None -> feature OFF
    notices = [e[1] for e in log if e[0] == "notify"]
    assert not any("possible secret" in m for m in notices)
    assert marker.read_text().strip() == "hi"


def test_other_tools_dont_get_the_param_notice(tmp_path):
    # the exception is run_command SPECIFICALLY — write_file's content isn't
    # scanned pre-write by this check (its eventual read-back still goes
    # through the normal tool-OUTPUT challenge, just not at write time)
    approve.save({"run_command": [], "write_file": ["*"], "edit_file": []})
    f = tmp_path / "o.txt"
    secret = "AKIA" + "N0T1C3N0T1C3N0T1"
    prov = FakeProvider([
        TurnResult(text="", tool_calls=[ToolCall("1", "write_file",
                   {"path": str(f), "content": secret})], stop_reason="tool_use"),
        TurnResult(text="all done", stop_reason="end"),
    ])
    log = []
    msgs = [{"role": "user", "content": "write it"}]
    agent.run_turn(prov, "m", msgs, "sys", _cb(log=log, secret_ans="keep"),
                   5, True, False)
    notices = [e[1] for e in log if e[0] == "notify"]
    assert not any("possible secret in this command" in m for m in notices)


class MalformThenOK:
    def __init__(self):
        self.n = 0
    def turn(self, *a, **k):
        self.n += 1
        if self.n == 1:
            raise __import__("aurora.providers.base", fromlist=["MalformedToolCall"]).MalformedToolCall("bad")
        return TurnResult(text="recovered", stop_reason="end")
    def assistant_message(self, r): return {"role": "assistant", "content": r.text}
    def tool_result_message(self, c, o): return {"role": "tool", "content": o}


def test_malformed_retry_leaves_no_nudge_in_history():
    msgs = [{"role": "user", "content": "hi"}]
    agent.run_turn(MalformThenOK(), "m", msgs, "s", _cb(), 5, True, False)
    # only the original user + the recovered assistant — no leaked nudge,
    # no two-user-in-a-row (which most chat APIs 400 on)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]


def test_cancel_between_iterations_stops():
    calls = {"n": 0}
    def cancel_after_first():
        calls["n"] += 1
        return calls["n"] > 1  # first check (top of loop) false, later true
    prov = FakeProvider([
        TurnResult(tool_calls=[ToolCall("1", "read_file", {"path": "/x"})],
                   stop_reason="tool_use") for _ in range(5)])
    cb = _cb()
    cb.cancelled = cancel_after_first
    msgs = [{"role": "user", "content": "go"}]
    t = agent.run_turn(prov, "m", msgs, "s", cb, 5, True, False)
    assert t.iterations <= 2  # stopped early, didn't run all 5


def test_iteration_cap_asks_and_stops():
    prov = FakeProvider([
        TurnResult(tool_calls=[ToolCall("1", "read_file", {"path": "/x"})],
                   stop_reason="tool_use")
        for _ in range(10)])
    msgs = [{"role": "user", "content": "loop"}]
    log = []
    t = agent.run_turn(prov, "m", msgs, "s", _cb(cont=False, log=log), 3, True, False)
    assert t.iterations == 3
    assert any("iteration cap" in m for _, m in [e for e in log if e[0] == "notify"])


class _UnreachableProvider:
    """Raises a connectivity ProviderError, like a timed-out remote API."""
    def __init__(self, name, base_url=""):
        self.name = name           # the raw config-key — must NEVER surface
        self.base_url = base_url
    def turn(self, *a, **k):
        from aurora.providers.base import ProviderError
        raise ProviderError(f"{self.name} request failed: The handshake "
                            "operation timed out")


def test_unreachable_message_classifies_remote_by_public_hostname():
    # a REMOTE backend (openrouter) timing out must not be blamed on the
    # local one — and must never echo the raw provider.name (a user's config
    # key), only its PUBLIC hostname (safe — not personal)
    log = []
    agent.run_turn(_UnreachableProvider("openrouter", "https://openrouter.ai/api/v1"),
                   "m", [{"role": "user", "content": "ls"}], "s",
                   _cb(log=log), 5, True, False)
    notices = [e[1] for e in log if e[0] == "notify"]
    assert any("openrouter.ai unreachable" in m for m in notices)
    assert not any("openrouter request failed" in m for m in notices)  # raw exception gone
    assert not any("work" in m and "OpenRouter" in m for m in notices)  # old misleading text gone


def test_unreachable_message_never_leaks_a_local_hostname():
    # a LAN/tailnet provider's base_url can bake in a personal hostname
    # (e.g. a Tailscale MagicDNS name) — the notice must say "local backend"
    # generically, never the actual host, and never the raw config-key name
    log = []
    agent.run_turn(
        _UnreachableProvider("my-private-name",
                            "https://someone-box.example-tailnet.ts.net:18182/v1"),
        "m", [{"role": "user", "content": "ls"}], "s",
        _cb(log=log), 5, True, False)
    notices = [e[1] for e in log if e[0] == "notify"]
    assert any("local backend unreachable" in m for m in notices)
    assert not any("someone-box" in m.lower() or "my-private-name" in m for m in notices)


class _RateLimitedProvider:
    """Raises a 429 ProviderError, like a free-tier OpenRouter model shared
    across too many concurrent users on its upstream."""
    def __init__(self, name, base_url=""):
        self.name = name
        self.base_url = base_url
    def turn(self, *a, **k):
        from aurora.providers.base import ProviderError
        raise ProviderError(
            'openrouter HTTP 429: {"error": {"message": "Provider returned '
            'error", "code": 429, "metadata": {"raw": "qwen/qwen3-coder:free '
            'is temporarily rate-limited upstream..."}}}')


def test_rate_limit_gets_actionable_hint_not_raw_json():
    # a 429 must not dump the raw provider JSON blob at the user — give the
    # same kind of targeted, human-readable hint as context-full/connectivity
    log = []
    agent.run_turn(_RateLimitedProvider("openrouter"),
                   "m", [{"role": "user", "content": "hi"}], "s",
                   _cb(log=log), 5, True, False)
    notices = [e[1] for e in log if e[0] == "notify"]
    assert any("rate-limited" in m for m in notices)
    assert not any('"metadata"' in m for m in notices)  # raw JSON gone


def test_remote_provider_gets_a_longer_connect_timeout():
    # a public API's TLS handshake can be slow — a 5s budget (fine for a LAN
    # server that's off) causes false handshake-timeouts, so remote gets more
    from aurora.providers.openai_compat import OpenAICompatProvider, _is_lan_host
    assert _is_lan_host("http://localhost:8080/v1")
    assert not _is_lan_host("https://openrouter.ai/api/v1")

    remote = OpenAICompatProvider("openrouter",
                                  {"base_url": "https://openrouter.ai/api/v1"}, 300)
    lan = OpenAICompatProvider("local",
                               {"base_url": "http://localhost:8080/v1"}, 300)
    assert remote._client.timeout.connect == 20
    assert lan._client.timeout.connect == 5
    # both connect with Happy Eyeballs (races IPv4/IPv6, first wins)
    from aurora.providers.happy_eyeballs import HappyEyeballsTransport
    assert isinstance(remote._client._transport, HappyEyeballsTransport)


def test_happy_eyeballs_prefers_the_reachable_family(monkeypatch):
    # a blackholed IPv6 listed first must not stall the connect: the reachable
    # IPv4 wins the race well before the (much longer) connect timeout
    import socket, time
    from aurora.providers import happy_eyeballs as he

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    monkeypatch.setattr(he, "_STAGGER", 0.05)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
        (socket.AF_INET,  socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
    ])
    t = time.time()
    s = he.happy_eyeballs_connect("blackhole.test", port, 10, None)
    elapsed = time.time() - t
    assert ":" not in s.getpeername()[0]      # won via IPv4
    assert elapsed < 2                          # nowhere near the 10s timeout
    s.close(); srv.close()


# ── bootstrap prompt (/bootstrap) ─────────────────────────────────────────
def test_bootstrap_project_overrides_global(tmp_path, monkeypatch):
    from aurora import bootstrap
    monkeypatch.setattr(bootstrap, "_global_path",
                        lambda: tmp_path / "bootstrap.md")
    proj = tmp_path / "proj"
    proj.mkdir()
    assert bootstrap.load(proj) == (None, None)
    bootstrap.save("global prompt")
    text, source = bootstrap.load(proj)
    assert text == "global prompt" and source.startswith("global")
    bootstrap.save("proj prompt", project=True, cwd=proj)
    text, source = bootstrap.load(proj)
    assert text == "proj prompt" and source.startswith("project")
    assert bootstrap.clear(project=True, cwd=proj)
    assert bootstrap.load(proj)[0] == "global prompt"


def test_bootstrap_empty_is_no_prompt(tmp_path, monkeypatch):
    from aurora import bootstrap
    monkeypatch.setattr(bootstrap, "_global_path",
                        lambda: tmp_path / "bootstrap.md")
    bootstrap.save("   \n")
    assert bootstrap.load(tmp_path) == (None, None)


def test_list_sessions_skips_bootstrap_turn():
    from aurora import session as sessions
    s = sessions.Session()
    s.log("bootstrap", source="global", chars=42)
    s.log("user", text="bootstrap boilerplate ritual", bootstrap=True)
    s.log("assistant", text="done", model="m")
    s.log("user", text="fix the login bug")
    rows = sessions.list_sessions()
    row = next(r for r in rows if r[0] == s.id)
    assert row[2] == "fix the login bug"
    s.log_path.unlink()


def test_bootstrap_from_input_path_snapshot(tmp_path):
    from aurora import bootstrap
    src = tmp_path / "boot.md"
    src.write_text("ritual contents\n")
    text, found = bootstrap.from_input(f"  {src}  ")
    assert text == "ritual contents\n" and found == src
    # plain prose (even mentioning a path) stays as-is
    text, found = bootstrap.from_input("read AGENTS.md\nthen tree")
    assert found is None and text.startswith("read AGENTS.md")
    text, found = bootstrap.from_input("/no/such/file.md")
    assert found is None and text == "/no/such/file.md"


def test_bootstrap_from_input_requires_md_or_txt(tmp_path):
    from aurora import bootstrap
    src = tmp_path / "boot.sh"
    src.write_text("#!/bin/sh\n")
    # exists but wrong extension -> treated as literal prompt text
    text, found = bootstrap.from_input(str(src))
    assert found is None and text == str(src)
    ok = tmp_path / "boot.TXT"
    ok.write_text("ritual\n")
    text, found = bootstrap.from_input(str(ok))
    assert found == ok and text == "ritual\n"


# ── last-model persistence ─────────────────────────────────────────────────
_CFG = """
providers:
  local: {type: openai, base_url: "http://x"}
  remote: {type: openai, base_url: "http://y", api_key_env: MISSING_KEY_XYZ}
models:
  - {model: m-one, provider: local}
  - {model: m-two, provider: local}
  - {model: remote-model, provider: remote}
"""


def _mk_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG)
    from aurora.engine import Engine
    return Engine(str(cfg))


def test_switch_model_is_remembered_across_restarts(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    assert e.current["model"] == "m-one"
    e.switch_model({"model": "m-two", "provider": "local"})
    e2 = _mk_engine(tmp_path, monkeypatch)
    assert e2.current["model"] == "m-two"


def test_library_model_restores_via_provider_clone(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    e.switch_model({"model": "lib-model-not-in-config", "provider": "local"})
    e2 = _mk_engine(tmp_path, monkeypatch)
    assert e2.current["model"] == "lib-model-not-in-config"
    assert e2.current["provider"] == "local"


def test_model_picker_finds_current_by_value_not_identity(tmp_path, monkeypatch):
    # switch_model() stores whatever dict it's handed — NOT the same object
    # as the matching entry in engine.list_models() — so the picker must
    # match current by (provider, model), not `is`, or it silently
    # pre-highlights/marks the WRONG entry as current
    from aurora import ui
    e = _mk_engine(tmp_path, monkeypatch)
    e.switch_model({"model": "m-two", "provider": "local"})
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # blank = accept default
    ui._pick_model(e, ui.TerminalFrontend())
    assert e.current["model"] == "m-two"   # unchanged: default_index pointed at IT


_CFG_LOCAL_NEEDS_KEY = """
providers:
  local: {type: openai, base_url: "http://x", api_key_env: MISSING_LOCAL_KEY_XYZ}
  opr:   {type: openai, base_url: "http://y", api_key_env: PRESENT_KEY_XYZ}
models:
  - {model: local, provider: local}
  - {model: gpt, provider: opr}
"""


def test_fresh_boot_skips_keyless_model_for_one_with_a_key(tmp_path, monkeypatch):
    # local is models[0] and NEEDS a key nobody has stored — a fresh boot (no
    # state.yaml) must not default onto it and nag for that key on every send;
    # it should land on the model that already has a usable key instead
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PRESENT_KEY_XYZ", "some-real-key")
    monkeypatch.delenv("MISSING_LOCAL_KEY_XYZ", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG_LOCAL_NEEDS_KEY)
    from aurora.engine import Engine
    e = Engine(str(cfg))
    assert e.current["provider"] == "opr"
    assert e.has_key("local") is False
    assert e.has_key("opr") is True


def test_fresh_boot_falls_back_to_first_model_if_none_have_keys(tmp_path, monkeypatch):
    # if NOTHING has a key yet, something has to be the default — keep the
    # original behavior (first configured model) rather than returning nothing
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MISSING_LOCAL_KEY_XYZ", raising=False)
    monkeypatch.delenv("PRESENT_KEY_XYZ", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG_LOCAL_NEEDS_KEY)
    from aurora.engine import Engine
    e = Engine(str(cfg))
    assert e.current["provider"] == "local"   # models[0], no better option exists


def test_model_picker_flags_entry_with_no_key_set(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PRESENT_KEY_XYZ", "some-real-key")
    monkeypatch.delenv("MISSING_LOCAL_KEY_XYZ", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG_LOCAL_NEEDS_KEY)
    from aurora.engine import Engine
    from aurora import ui
    e = Engine(str(cfg))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")   # accept default
    ui._pick_model(e, ui.TerminalFrontend())
    out = capsys.readouterr().out
    # the keyless local entry is annotated; the one with a real key is not
    local_line = next(l for l in out.splitlines() if "local" in l and "gpt" not in l)
    gpt_line = next(l for l in out.splitlines() if "gpt" in l)
    assert "no key set" in local_line
    assert "no key set" not in gpt_line
    # current stays on opr (has a key); local must never have been selected
    assert e.current["provider"] == "opr"


def test_model_picker_prompts_for_key_right_after_picking_a_keyless_model(
        tmp_path, monkeypatch):
    # selecting an entry marked "(no key set)" must let the user enter it
    # immediately, not just annotate the problem and leave them to hunt for
    # `aurora key set` separately
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PRESENT_KEY_XYZ", "some-real-key")
    monkeypatch.delenv("MISSING_LOCAL_KEY_XYZ", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG_LOCAL_NEEDS_KEY)
    from aurora import keystore, ui
    from aurora.engine import Engine

    # in-memory fake keyring — never touches the real OS keychain
    fake_store: dict = {}
    monkeypatch.setattr(keystore, "_keyring_get",
                        lambda name: fake_store.get(name))
    monkeypatch.setattr(keystore, "_keyring_set",
                        lambda name, value: fake_store.__setitem__(name, value) or True)

    e = Engine(str(cfg))
    assert e.current["provider"] == "opr"   # local skipped at boot (no key)

    # pick "local" ("gpt" sorts first alphabetically) then enter its key
    answers = iter(["2", "freshly-entered-key"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))

    ui._pick_model(e, ui.TerminalFrontend())

    assert e.current["provider"] == "local"
    assert fake_store.get("MISSING_LOCAL_KEY_XYZ") == "freshly-entered-key"
    assert e.has_key("local") is True   # cache invalidated, sees the fresh key


def test_llamadesk_unreachable_is_cached_briefly(monkeypatch):
    # /model must feel instant like /exit — a recently-unreachable LlamaDesk
    # must not make every subsequent /model pay the ~5s probe timeout again
    from aurora import ui
    class _E:
        cfg = {"llamadesk": {"url": "https://example-unreachable.invalid:9"}}
    ui._llamadesk_last_fail.clear()
    e = _E()
    assert ui._llamadesk(e) is not None    # first call: probe is attempted
    ui._llamadesk_mark_failed("https://example-unreachable.invalid:9")
    assert ui._llamadesk(e) is None          # cached: skipped, no new probe
    # TTL expiry re-enables the probe
    monkeypatch.setattr(ui, "_LLAMADESK_RECHECK_S", -1)
    assert ui._llamadesk(e) is not None


# ── R68: context-size picker on a library load ──────────────────────────────
def test_pick_ctx_offers_64_128_256(monkeypatch):
    from aurora import ui
    monkeypatch.setattr("builtins.input", lambda *a, **k: "131072")
    ctx = ui._pick_ctx(default_ctx=65536, native=None)
    assert ctx == 131_072


def test_pick_ctx_drops_options_over_native(monkeypatch):
    from aurora import ui
    # native=100000 sits between 64k and 128k — 128k/256k must be dropped
    # entirely (never rope-extend past what the model was trained for),
    # leaving only 64k offered
    seen = {}
    def fake_select(prompt, options, default_index=None):
        seen["options"] = options
        return options[0][0]
    monkeypatch.setattr(ui, "select", fake_select)
    ctx = ui._pick_ctx(default_ctx=65536, native=100_000)
    assert ctx == 65536
    assert seen["options"] == [("65536", "64k")]


def test_pick_ctx_native_below_64k_offers_native_alone(monkeypatch):
    from aurora import ui
    # a tiny model's native ctx is under even the smallest rung — offer it
    # directly instead of an empty menu
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    ctx = ui._pick_ctx(default_ctx=65536, native=8192)
    assert ctx == 8192


def test_pick_ctx_default_targets_native_cap_not_configured_value(monkeypatch):
    from aurora import ui
    # configured default (65536) is ABOVE this model's native (32768) — with
    # only 64k offered (128k/256k dropped), the pre-selected rung must still
    # resolve to the one valid option, not crash on an out-of-range index
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    ctx = ui._pick_ctx(default_ctx=65536, native=32_768)
    assert ctx == 32_768


class _FakeDesk:
    """Stands in for LlamaDesk in _pick_model's library-load branch."""
    def __init__(self, models_detail, loaded, switch_calls):
        self._detail = models_detail
        self._loaded = loaded
        self._switch_calls = switch_calls
        self.base_url = "http://fake"

    def loaded_model(self):
        return self._loaded

    def models_detail(self):
        return self._detail

    def busy(self):
        return False

    def switch(self, name, ctx=65536, ngl="auto"):
        self._switch_calls.append((name, ctx))
        self._loaded = name

    def wait_ready(self, name, poll=0.0, timeout=1, on_tick=None):
        return True


def test_pick_model_library_load_prompts_for_ctx_and_passes_it_through(
        tmp_path, monkeypatch):
    from aurora import ui
    e = _mk_engine(tmp_path, monkeypatch)
    e.cfg["llamadesk"] = {"ctx": 65536}
    switch_calls = []
    desk = _FakeDesk(
        models_detail=[{"name": "big-model.gguf", "ctx_native": 131_072,
                        "size_bytes": 1}],
        loaded="m-one", switch_calls=switch_calls)
    monkeypatch.setattr(ui, "_llamadesk", lambda engine: desk)
    # menu order: pick "local:big-model.gguf" from the model list (matched by
    # label substring — its options are numbered, not named), then ctx=128k
    # (native=131072 caps out 256k, leaving 64k/128k — pick the second).
    # Eviction confirm is mocked directly below, so it consumes no input.
    inputs = iter(["local:big-model.gguf", "131072"])

    def fake_select(prompt, options, default_index=None):
        raw = next(inputs)
        for k, _ in options:
            if k == raw:
                return k
        for k, label in options:
            if raw in label:
                return k
        raise AssertionError(f"no option matched {raw!r} in {options}")

    monkeypatch.setattr(ui, "select", fake_select)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    ui._pick_model(e, ui.TerminalFrontend())
    assert switch_calls == [("big-model.gguf", 131_072)]
    assert e.current["model"] == "big-model.gguf"


def test_last_model_without_key_falls_back_to_default(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    e.switch_model({"model": "remote-model", "provider": "remote"})
    e2 = _mk_engine(tmp_path, monkeypatch)
    assert e2.current["model"] == "m-one"   # remote key missing → default


# ── R58: secret detection in the USER PROMPT (Engine.send) ─────────────────
class _FakeFE:
    def __init__(self, secret_ans):
        self.secret_ans = secret_ans
        self.notices: list[str] = []

    def on_text(self, *a): pass
    def on_tool_start(self, *a): pass
    def on_tool_result(self, *a): pass
    def approve(self, *a): return "y"
    def ask_continue(self, *a): return True
    def notify(self, m): self.notices.append(m)
    def cancelled(self): return False
    def secret_challenge(self, context, matches, **_k): return self.secret_ans


_AWS_KEY = "AKIA" + "R2R2R2R2R2R2R2R2"   # 16 chars after the prefix


def _stub_run_turn(provider, model, messages, system, cb, *a, **k):
    # a real run_turn appends the assistant reply to `messages`; without that,
    # engine.send()'s "turn produced nothing" cleanup pops the user message
    # right back off (see engine.py) — mimic a minimal successful turn so the
    # user message these tests are inspecting actually stays in history
    messages.append({"role": "assistant", "content": "ok"})
    return agent.Turn()


def test_send_stop_blocks_prompt_with_a_secret(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: (_ for _ in ())
                        .throw(AssertionError("must not reach the model")))
    fe = _FakeFE("stop")
    e.send(f"here is my key {_AWS_KEY}", fe)
    assert e.messages == []                          # never entered history
    assert any("secret" in n for n in fe.notices)


def test_send_redact_scrubs_prompt_before_history(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "run_turn", _stub_run_turn)
    fe = _FakeFE("redact")
    e.send(f"here is my key {_AWS_KEY}", fe)
    assert _AWS_KEY not in str(e.messages[0])
    assert "<secret>" in str(e.messages[0])


def test_send_keep_sends_prompt_unchanged(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "run_turn", _stub_run_turn)
    fe = _FakeFE("keep")
    e.send(f"here is my key {_AWS_KEY}", fe)
    assert _AWS_KEY in str(e.messages[0])


def test_send_skips_scan_when_redact_disabled(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    e.redact_secrets = False
    monkeypatch.setattr(agent, "run_turn", _stub_run_turn)
    class _NoChallengeFE(_FakeFE):
        def secret_challenge(self, context, matches, **_k):
            raise AssertionError("must not be called when redact_secrets is off")
    e.send(f"here is my key {_AWS_KEY}", _NoChallengeFE("keep"))
    assert _AWS_KEY in str(e.messages[0])             # unscanned, sent as-is


def test_redact_secrets_persists_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_CFG)                         # written ONCE — the
    from aurora.engine import Engine                  # engine's own writeback
    e = Engine(str(cfg_path))                          # must survive a reread
    assert e.redact_secrets is True                    # default ON
    e.set_redact_secrets(False)
    e2 = Engine(str(cfg_path))
    assert e2.redact_secrets is False


def test_send_always_allowlists_and_sends_unchanged(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "run_turn", _stub_run_turn)
    fe = _FakeFE("always")
    e.send(f"here is my key {_AWS_KEY}", fe)
    assert _AWS_KEY in str(e.messages[0])              # sent as-is, like 'keep'
    assert secrets.hash_value(_AWS_KEY) in e.secret_allowlist
    assert any("allowlist" in n for n in fe.notices)


def test_send_does_not_challenge_an_allowlisted_secret_again(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    e.secret_allowlist = {secrets.hash_value(_AWS_KEY)}
    monkeypatch.setattr(agent, "run_turn", _stub_run_turn)
    class _NoChallengeFE(_FakeFE):
        def secret_challenge(self, context, matches, **_k):
            raise AssertionError("must not be re-challenged once allowlisted")
    e.send(f"here is my key {_AWS_KEY}", _NoChallengeFE("keep"))
    assert _AWS_KEY in str(e.messages[0])


def test_secret_allowlist_persists_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_CFG)
    from aurora.engine import Engine
    e = Engine(str(cfg_path))
    e.add_secret_allowlist_entries([_AWS_KEY])
    e2 = Engine(str(cfg_path))
    assert secrets.hash_value(_AWS_KEY) in e2.secret_allowlist
    e2.clear_secret_allowlist()
    e3 = Engine(str(cfg_path))
    assert e3.secret_allowlist == set()


class MalformThenError:
    """Malformed first call; the RETRY fails with a ProviderError."""
    def turn(self, *a, **k):
        from aurora.providers.base import MalformedToolCall, ProviderError
        if not getattr(self, "n", 0):
            self.n = 1
            raise MalformedToolCall("bad")
        raise ProviderError("boom")
    def assistant_message(self, r): return {"role": "assistant", "content": r.text}
    def tool_result_message(self, c, o): return {"role": "tool", "content": o}


def test_malformed_retry_error_leaves_no_nudge_in_history():
    import pytest
    from aurora.providers.base import ProviderError
    msgs = [{"role": "user", "content": "hi"}]
    with pytest.raises(ProviderError):
        agent.run_turn(MalformThenError(), "m", msgs, "s", _cb(), 5, True, False)
    # the corrective nudge must not survive a failed retry — it would sit in
    # history as a second consecutive user message and poison the next send
    assert [m["role"] for m in msgs] == ["user"]


def test_diff_preview_never_raises_on_binary_target(tmp_path):
    # a write over a non-UTF8 file used to raise UnicodeDecodeError from
    # inside the agent loop, AFTER the assistant tool_use was in history —
    # killing the turn and poisoning every later request
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\xff\xfe\x00garbage\x9c")
    out = approve.diff_preview("write_file", {"path": str(p), "content": "x"})
    assert isinstance(out, str) and "diff unavailable" in out
    out = approve.diff_preview("edit_file", {"path": str(p), "old": "a", "new": "b"})
    assert isinstance(out, str) and "diff unavailable" in out


def test_sse_stream_skips_garbled_line(monkeypatch):
    from aurora.providers import openai_compat as oc
    prov = oc.OpenAICompatProvider("x", {"base_url": "http://127.0.0.1:9"}, 5)
    monkeypatch.setattr(prov, "pick_endpoint", lambda cache_ok=True: prov.base_url)

    def fake_sse(open_stream, cancel, poll=0.15):
        yield ("status", 200, None)
        yield ("line", "data: {truncated-garbage", None)     # must be skipped
        yield ("line", 'data: {"choices":[{"delta":{"content":"hi"}}]}', None)
        yield ("line", "data: [DONE]", None)

    monkeypatch.setattr(oc, "cancellable_sse", fake_sse)
    got = []
    r = prov.turn("m", [{"role": "user", "content": "q"}], "", None,
                  got.append, lambda: False)
    assert r.text == "hi" and got == ["hi"]


# ── /model add (R80) ───────────────────────────────────────────────────────
def test_parse_openrouter_model():
    from aurora import ui
    assert ui._parse_openrouter_model(
        "https://openrouter.ai/kwaipilot/kat-coder-air-v2.5") == "kwaipilot/kat-coder-air-v2.5"
    assert ui._parse_openrouter_model(
        "https://openrouter.ai/models/kwaipilot/kat-coder-air-v2.5/") == "kwaipilot/kat-coder-air-v2.5"
    assert ui._parse_openrouter_model("kwaipilot/kat-coder-air-v2.5") == "kwaipilot/kat-coder-air-v2.5"
    assert ui._parse_openrouter_model("no-slash-id") is None
    assert ui._parse_openrouter_model("two words/here x") is None
    assert ui._parse_openrouter_model("") is None


_OR_CFG = """
providers:
  openrouter:
    type: openai
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
models:
  - provider: openrouter
    model: existing/model
    tools: true
"""


def _mk_or_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_OR_CFG)
    from aurora.engine import Engine
    return Engine(str(cfg)), cfg


def test_add_model_persists_and_dedupes(tmp_path, monkeypatch):
    import yaml
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    entry, created = e.add_model("kwaipilot/kat-coder-air-v2.5")
    assert created and entry in e.models
    on_disk = yaml.safe_load(cfg.read_text())["models"]
    assert {"provider": "openrouter", "model": "kwaipilot/kat-coder-air-v2.5",
            "tools": True} in on_disk
    # adding again: no duplicate, nothing rewritten
    entry2, created2 = e.add_model("kwaipilot/kat-coder-air-v2.5")
    assert not created2 and entry2 is entry
    assert len(yaml.safe_load(cfg.read_text())["models"]) == len(on_disk)


def test_save_remote_model_info_updates_json_and_memory(tmp_path, monkeypatch):
    import json as _json
    from aurora.providers import openai_compat as oc
    path = tmp_path / "limits.json"
    path.write_text("[]")
    monkeypatch.setattr(oc, "_REMOTE_CONTEXT_LIMITS_PATH", path)
    monkeypatch.setattr(oc, "REMOTE_CONTEXT_LIMITS", {})
    oc.save_remote_model_info("kwaipilot/kat-coder-air-v2.5",
                              {"context_size": 262144,
                               "price_in_per_mtok": 0.044,
                               "price_out_per_mtok": 0.599,
                               "description": "Agentic coding model."})
    entry = _json.loads(path.read_text())[0]
    assert entry["model"] == "kwaipilot/kat-coder-air-v2.5"
    assert entry["provider"] == "openrouter"
    assert entry["code"] == "kat-coder-air-v2.5"
    assert entry["context_size"] == 262144
    assert entry["price_in_per_mtok"] == 0.044
    assert entry["description"] == "Agentic coding model."
    assert "openrouter.ai/kwaipilot/kat-coder-air-v2.5#pricing" in entry["pricing_url"]
    # in-memory table sees it too (footer $ badge works without a restart)
    assert oc.REMOTE_CONTEXT_LIMITS["kwaipilot/kat-coder-air-v2.5"]["context_size"] == 262144


def test_model_add_command_end_to_end(tmp_path, monkeypatch, capsys):
    import yaml
    from aurora import ui
    from aurora.providers import openai_compat as oc
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(oc, "fetch_openrouter_model_info",
                        lambda mid: ({"context_size": 262144,
                                      "price_in_per_mtok": 0.044,
                                      "price_out_per_mtok": 0.599,
                                      "description": "d"}, True))
    saved = {}
    monkeypatch.setattr(oc, "save_remote_model_info",
                        lambda mid, info: saved.update({mid: info}))
    ui._handle_command(e, None,
                       "/model add https://openrouter.ai/kwaipilot/kat-coder-air-v2.5")
    out = capsys.readouterr().out
    assert "added kwaipilot/kat-coder-air-v2.5" in out
    assert "ctx 262k" in out and "$0.044/$0.599" in out
    assert e.current["model"] == "kwaipilot/kat-coder-air-v2.5"   # switched
    assert "kwaipilot/kat-coder-air-v2.5" in saved
    on_disk = [m["model"] for m in yaml.safe_load(cfg.read_text())["models"]]
    assert "kwaipilot/kat-coder-air-v2.5" in on_disk


def test_model_add_rejects_garbage(tmp_path, monkeypatch, capsys):
    from aurora import ui
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    ui._handle_command(e, None, "/model add not-a-model")
    assert "usage: /model add" in capsys.readouterr().out
    assert e.current["model"] == "existing/model"


# ── /model remove (R81) ────────────────────────────────────────────────────
def test_remove_model_persists(tmp_path, monkeypatch):
    import yaml
    from aurora import ui
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    e.add_model("kwaipilot/kat-coder-air-v2.5")
    removed, new_current = e.remove_model("kwaipilot/kat-coder-air-v2.5")
    assert removed == 1 and new_current is None      # wasn't the current model
    names = [m["model"] for m in yaml.safe_load(cfg.read_text())["models"]]
    assert names == ["existing/model"]
    assert [m["model"] for m in e.models] == ["existing/model"]  # live list too


def test_remove_current_model_falls_back(tmp_path, monkeypatch):
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    entry, _ = e.add_model("kwaipilot/kat-coder-air-v2.5")
    e.switch_model(entry)
    removed, new_current = e.remove_model("kwaipilot/kat-coder-air-v2.5")
    assert removed == 1
    assert new_current["model"] == "existing/model"
    assert e.current["model"] == "existing/model"


def test_remove_last_model_leaves_no_current(tmp_path, monkeypatch):
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    removed, new_current = e.remove_model("existing/model")
    assert removed == 1 and new_current == {} and e.current == {}


def test_remove_model_command_accepts_url_and_unknown(tmp_path, monkeypatch, capsys):
    from aurora import ui
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    e.add_model("kwaipilot/kat-coder-air-v2.5")
    ui._handle_command(e, None,
                       "/model remove https://openrouter.ai/kwaipilot/kat-coder-air-v2.5")
    out = capsys.readouterr().out
    assert "removed kwaipilot/kat-coder-air-v2.5" in out
    ui._handle_command(e, None, "/model rm nobody/nothing")
    assert "not in config.yaml" in capsys.readouterr().out


def test_model_add_refuses_nonexistent_model(tmp_path, monkeypatch, capsys):
    import yaml
    from aurora import ui
    from aurora.providers import openai_compat as oc
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    # catalog reachable, model not in it → refuse, nothing added
    monkeypatch.setattr(oc, "fetch_openrouter_model_info", lambda mid: (None, True))
    ui._handle_command(e, None, "/model add nobody/does-not-exist")
    out = capsys.readouterr().out
    assert "not found on OpenRouter" in out
    names = [m["model"] for m in yaml.safe_load(cfg.read_text())["models"]]
    assert names == ["existing/model"]
    assert e.current["model"] == "existing/model"


def test_model_add_offline_adds_unverified(tmp_path, monkeypatch, capsys):
    import yaml
    from aurora import ui
    from aurora.providers import openai_compat as oc
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    # catalog unreachable → can't verify, add anyway with a warning
    monkeypatch.setattr(oc, "fetch_openrouter_model_info", lambda mid: (None, False))
    ui._handle_command(e, None, "/model add kwaipilot/kat-coder-air-v2.5")
    out = capsys.readouterr().out
    assert "added kwaipilot/kat-coder-air-v2.5" in out
    assert "unverified" in out
    names = [m["model"] for m in yaml.safe_load(cfg.read_text())["models"]]
    assert "kwaipilot/kat-coder-air-v2.5" in names


def test_send_with_no_model_notifies_instead_of_crashing(tmp_path, monkeypatch):
    e, cfg = _mk_or_engine(tmp_path, monkeypatch)
    e.remove_model("existing/model")           # last model gone (R81)
    notes = []
    class _FE:
        def notify(self, m): notes.append(m)
    e.send("hello", _FE())
    assert any("no model selected" in m for m in notes)
    assert e.messages == []                     # nothing appended, no crash


# ── keystore: non-interactive lookup must never prompt ────────────────────
def test_get_key_noninteractive_never_prompts_for_passphrase(tmp_path, monkeypatch):
    """With an encrypted key file present but no cached passphrase, a
    non-interactive get_key (footer/picker path) must return None, not block
    on a hidden passphrase prompt."""
    from aurora import keystore
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.delenv("ENCSTORED_VAR", raising=False)
    monkeypatch.setattr(keystore, "_keyring_get", lambda n: None)
    (tmp_path / "keys.enc").write_bytes(b"garbage")   # file exists, locked
    keystore._passphrase_cache.clear()
    prompts = []
    monkeypatch.setattr(keystore, "_prompter",
                        lambda label: prompts.append(label) or "")
    assert keystore.get_key("ENCSTORED_VAR", interactive=False) is None
    assert prompts == []                                # never asked
