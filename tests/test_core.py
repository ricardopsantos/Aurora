"""Core tests — no network; providers are faked. Run: python -m pytest tests/"""

import os
import tempfile
import threading
import time
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


# ── R96h: _norm_command must not re-shlex the same rule on every check ────
def test_norm_command_caches_by_input_string(monkeypatch):
    """R96h: is_allowed calls _norm_command once per rule per check, so an
    allowlist with N rules gets each rule re-shlex.split()'d on every one of
    a turn's tool calls even though the rule strings never change between
    calls. lru_cache turns repeated calls with the SAME string into a single
    real tokenization."""
    from aurora import approve
    approve._norm_command.cache_clear()
    real_split = approve.shlex.split
    calls = {"n": 0}

    def counting_split(s):
        calls["n"] += 1
        return real_split(s)

    monkeypatch.setattr(approve.shlex, "split", counting_split)
    for _ in range(50):
        approve._norm_command("git status --short")
    assert calls["n"] == 1, f"shlex.split called {calls['n']} times for 50 identical inputs"
    approve._norm_command.cache_clear()


def test_norm_command_cache_does_not_change_correctness(monkeypatch):
    """The cache must be transparent — two different rule strings still get
    their own correct tokenization, and repeating a call after other calls
    still returns the right (cached) answer."""
    from aurora import approve
    approve._norm_command.cache_clear()
    assert approve._norm_command("git status") == ("git", "status")
    assert approve._norm_command("bash ~/x.sh") == ("bash", os.path.expanduser("~/x.sh"))
    assert approve._norm_command("git status") == ("git", "status")  # still correct, cached


def test_is_allowed_re_shlexes_each_rule_once_per_check_not_more(monkeypatch):
    """R96h in context: is_allowed over a realistic allowlist must not
    re-tokenize a rule more than once per DISTINCT rule string across many
    checks — the actual regression this fix targets."""
    from aurora import approve
    approve._norm_command.cache_clear()
    data = {"run_command": [f"cmd{i} sub" for i in range(50)],
            "write_file": [], "edit_file": []}
    real_split = approve.shlex.split
    calls = {"n": 0}

    def counting_split(s):
        calls["n"] += 1
        return real_split(s)

    monkeypatch.setattr(approve.shlex, "split", counting_split)
    for _ in range(20):   # 20 tool calls in a turn, same allowlist each time
        approve.is_allowed("run_command", {"command": "git status --short"}, data)
    # 50 rules + the incoming signature, tokenized once each, ever — not
    # 50*20 + 20
    assert calls["n"] == 51, f"expected 51 tokenizations, got {calls['n']}"
    approve._norm_command.cache_clear()


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


# ── R99: 429 gets its own backoff-and-retry, distinct from connection retry ─
def test_turn_retries_a_429_with_backoff_then_succeeds(monkeypatch):
    from aurora.providers import openai_compat as oc
    calls = {"n": 0}
    slept = []

    def fake_sse(open_stream, cancel, poll=0.15):
        calls["n"] += 1
        if calls["n"] < 3:
            yield ("status", 429, "rate limited")
        else:
            yield from _sse_ok("hi")

    monkeypatch.setattr(oc, "cancellable_sse", fake_sse)
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    res = _mk_provider().turn("m", [{"role": "user", "content": "x"}], "", None,
                              lambda t: None, lambda: False)
    assert calls["n"] == 3            # 2 x 429, then succeeded on the 3rd
    assert res.text == "hi"
    assert slept == list(oc._RATE_LIMIT_BACKOFF)   # backoff schedule, in order


def test_turn_gives_up_after_repeated_429s_with_a_clear_message(monkeypatch):
    from aurora.providers import openai_compat as oc
    from aurora.providers.base import ProviderError
    calls = {"n": 0}

    def always_429(open_stream, cancel, poll=0.15):
        calls["n"] += 1
        yield ("status", 429, "rate limited")

    monkeypatch.setattr(oc, "cancellable_sse", always_429)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(ProviderError, match="429"):
        _mk_provider().turn("m", [{"role": "user", "content": "x"}], "", None,
                            lambda t: None, lambda: False)
    assert calls["n"] == 3            # exhausted every attempt


def test_429_backoff_is_distinct_from_connection_retry_timing(monkeypatch):
    """429s must not share the connection-retry's flat 0.3*(attempt+1)
    schedule — a shared free-tier quota and a stale pooled connection reset
    are different problems with different right wait times."""
    from aurora.providers import openai_compat as oc
    calls = {"n": 0}
    slept = []

    def fake_sse(open_stream, cancel, poll=0.15):
        calls["n"] += 1
        if calls["n"] == 1:
            yield ("status", 429, "rate limited")
        else:
            yield from _sse_ok("hi")

    monkeypatch.setattr(oc, "cancellable_sse", fake_sse)
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    _mk_provider().turn("m", [{"role": "user", "content": "x"}], "", None,
                        lambda t: None, lambda: False)
    assert slept == [oc._RATE_LIMIT_BACKOFF[0]]
    assert slept[0] != 0.3 * 1   # not the connection-retry's schedule


def test_agent_classifies_a_repeated_429_as_rate_limited_not_a_raw_error(monkeypatch):
    """agent.py's ProviderError message classifier (both "429" and
    "rate"+"limit") must still recognize R99's exhausted-retry message, so
    the user still gets the friendly shared-quota notice, not the raw
    provider error text."""
    from aurora import agent
    from aurora.providers.base import ProviderError

    class _FakeProvider:
        base_url = ""

        def turn(self, *a, **k):
            raise ProviderError("openrouter rate-limited (429): rate limited")

        def assistant_message(self, result):
            return {}

    notes = []
    cb = agent.AgentCallbacks(
        on_text=lambda c: None, on_tool_start=lambda n, a: None,
        on_tool_result=lambda n, o: None, approve=lambda *a: "y",
        ask_continue=lambda n: True, notify=notes.append,
        cancelled=lambda: False)
    agent.run_turn(_FakeProvider(), "m", [], "", cb, 5, True, False)
    assert any("shared" in n and "free" in n for n in notes)


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


def test_turn_does_not_reprobe_on_every_iteration(monkeypatch):
    """R95h: turn() runs once per agent ITERATION, not once per user message,
    so forcing a probe here cost a round trip per tool round. The short TTL
    still re-probes between messages, and a connection failure expires it."""
    from aurora.providers import openai_compat as oc
    from aurora.providers.openai_compat import OpenAICompatProvider

    prov = OpenAICompatProvider(
        "local", {"base_url": ["http://10.0.0.5:8080/v1",
                               "http://10.0.0.6:8080/v1"]}, 300)
    prov._client_for = lambda base: object()
    probes = {"n": 0}

    def counting_probe(url):
        probes["n"] += 1
        return True

    monkeypatch.setattr(prov, "_probe", counting_probe)
    monkeypatch.setattr(oc, "cancellable_sse",
                        lambda open_stream, cancel, poll=0.15: iter(_sse_ok()))

    for _ in range(5):          # one turn, five tool iterations
        prov.turn("m", [{"role": "user", "content": "x"}], "", None,
                  lambda t: None, lambda: False)
    assert probes["n"] == 1, f"{probes['n']} probes for 5 iterations"

    # a connection failure must still force failover on the next request
    prov._working_url_at = 0.0
    prov.turn("m", [{"role": "user", "content": "x"}], "", None,
              lambda t: None, lambda: False)
    assert probes["n"] == 2


def test_probe_reuses_the_pooled_client(monkeypatch):
    """R95h: a bare httpx.get built a fresh client — and so a fresh TCP+TLS
    handshake — for every probe, which is most of what a probe costs."""
    from aurora.providers import openai_compat as oc
    from aurora.providers.openai_compat import OpenAICompatProvider

    prov = OpenAICompatProvider("local", {"base_url": "http://10.0.0.5:8080/v1"}, 300)
    used = []

    class _Resp:
        def raise_for_status(self):
            return None

    class _Client:
        def get(self, url, **kw):
            used.append(url)
            return _Resp()

    monkeypatch.setattr(prov, "_client_for", lambda base: _Client())
    monkeypatch.setattr(oc.httpx, "get", _bare_get_is_forbidden)
    assert prov._probe("http://10.0.0.5:8080/v1") is True
    assert used == ["http://10.0.0.5:8080/props"]


def _bare_get_is_forbidden(*a, **k):
    raise AssertionError("probe built a new client instead of reusing the pool")


# ── R96j: the loser of a _client_for race must not leak its httpx.Client ──
def test_client_for_closes_the_losing_client_on_a_race(monkeypatch):
    """R96j: `client = self._http.setdefault(base_url, new_client)` returns
    the WINNER's client when two threads race to build one for the same
    endpoint — correct — but the loser's freshly built httpx.Client (and its
    connection pool: sockets, not just Python memory) used to become
    unreachable from anywhere except the local variable that built it,
    leaking for the life of the process. This forces the race deterministically:
    a second call arrives while the first is still "building" (held open by
    a barrier) so the dict already has an entry by the time the first
    finishes constructing its own client."""
    from aurora.providers.openai_compat import OpenAICompatProvider
    import httpx as real_httpx
    import threading

    prov = OpenAICompatProvider("local", {"base_url": "http://10.0.0.5:8080/v1"}, 300)
    closed = []
    built = []
    release_first = threading.Event()
    first_building = threading.Event()

    class _TrackedClient:
        def close(self):
            closed.append(self)

    real_client_ctor = real_httpx.Client

    def slow_first_then_fast(*a, **kw):
        c = _TrackedClient()
        built.append(c)
        if len(built) == 1:
            first_building.set()
            release_first.wait(2)   # hold the "construction" open
        return c

    monkeypatch.setattr(real_httpx, "Client", slow_first_then_fast)

    results = {}

    def call_first():
        results["first"] = prov._client_for("http://10.0.0.5:8080/v1")

    def call_second():
        first_building.wait(2)
        results["second"] = prov._client_for("http://10.0.0.5:8080/v1")
        release_first.set()

    t1 = threading.Thread(target=call_first)
    t2 = threading.Thread(target=call_second)
    t1.start()
    t2.start()
    t1.join(timeout=3)
    t2.join(timeout=3)

    assert len(built) == 2, "test setup didn't force two constructions"
    assert results["first"] is results["second"], \
        "both callers must end up with the SAME (winning) client"
    loser = next(c for c in built if c is not results["first"])
    assert loser in closed, "the losing client was never closed — leaked"
    assert results["first"] not in closed, "the WINNING client must stay open"


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


def test_bootstrap_is_url():
    from aurora import bootstrap
    assert bootstrap.is_url("https://example.com/boot.md")
    assert bootstrap.is_url("http://example.com/boot.md")
    assert not bootstrap.is_url("not a url")
    assert not bootstrap.is_url("https://example.com/a\nhttps://example.com/b")


def test_bootstrap_save_with_source_url_round_trips(tmp_path, monkeypatch):
    from aurora import bootstrap
    monkeypatch.setattr(bootstrap, "_global_path",
                        lambda: tmp_path / "bootstrap.md")
    bootstrap.save("v1 from the web", source_url="https://example.com/boot.md")
    text, source = bootstrap.load(tmp_path)
    assert text == "v1 from the web"
    assert bootstrap.source_url(tmp_path) == "https://example.com/boot.md"
    # overwriting with a plain paste (no source_url) must drop the stale URL
    bootstrap.save("v2 pasted by hand")
    assert bootstrap.source_url(tmp_path) is None


def test_bootstrap_clear_removes_source_url_sidecar(tmp_path, monkeypatch):
    from aurora import bootstrap
    monkeypatch.setattr(bootstrap, "_global_path",
                        lambda: tmp_path / "bootstrap.md")
    bootstrap.save("from web", source_url="https://example.com/boot.md")
    assert bootstrap.clear()
    assert bootstrap.load(tmp_path) == (None, None)
    assert bootstrap.source_url(tmp_path) is None


def test_bootstrap_refresh_from_source_redownloads_and_persists(tmp_path, monkeypatch):
    from aurora import bootstrap
    monkeypatch.setattr(bootstrap, "_global_path",
                        lambda: tmp_path / "bootstrap.md")
    bootstrap.save("stale content", source_url="https://example.com/boot.md")
    monkeypatch.setattr(bootstrap, "fetch_url", lambda url: "fresh content")
    text, path = bootstrap.refresh_from_source(tmp_path)
    assert text == "fresh content"
    assert bootstrap.load(tmp_path)[0] == "fresh content"
    assert bootstrap.source_url(tmp_path) == "https://example.com/boot.md"


def test_bootstrap_refresh_from_source_none_when_not_url_sourced(tmp_path, monkeypatch):
    from aurora import bootstrap
    monkeypatch.setattr(bootstrap, "_global_path",
                        lambda: tmp_path / "bootstrap.md")
    bootstrap.save("pasted, not from a url")
    assert bootstrap.refresh_from_source(tmp_path) is None


def test_bootstrap_run_choice_offers_download_only_for_url(monkeypatch):
    from aurora import ui
    # no URL -> plain yes/no, no re-download option ever offered
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    assert ui._bootstrap_run_choice(None) == "run"
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: False)
    assert ui._bootstrap_run_choice(None) == "skip"
    # URL -> a 3-way select(), default "run" (the cached copy)
    seen = {}
    def fake_select(prompt, options, default_index=None):
        seen["keys"] = [k for k, _ in options]
        return options[default_index][0]
    monkeypatch.setattr(ui, "select", fake_select)
    assert ui._bootstrap_run_choice("https://example.com/boot.md") == "run"
    assert seen["keys"] == ["run", "download", "skip"]


def test_agentic_report_cmd_no_context_found(tmp_path, monkeypatch, capsys):
    from aurora import ui
    monkeypatch.chdir(tmp_path)
    ui._agentic_report_cmd(None, None)
    assert "no context protocol folder" in capsys.readouterr().out


def _init_repo(d, initial="v1\n"):
    import subprocess
    d.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=d, check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "test"], check=True)
    (d / "a.txt").write_text(initial)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-m", "initial"], check=True)
    return d


# ── R101: /commit ───────────────────────────────────────────────────────
def test_commit_cmd_not_a_repo(tmp_path, monkeypatch, capsys):
    from aurora import ui
    monkeypatch.chdir(tmp_path)
    ui._commit_cmd(None, None, "")
    assert "not a git repository" in capsys.readouterr().out


def test_commit_cmd_nothing_to_commit(tmp_path, monkeypatch, capsys):
    from aurora import ui
    repo = _init_repo(tmp_path / "proj")
    monkeypatch.chdir(repo)
    ui._commit_cmd(None, None, "")
    assert "working tree clean" in capsys.readouterr().out


def test_commit_cmd_staged_change_with_explicit_message(tmp_path, monkeypatch, capsys):
    """A message passed as the /commit argument skips drafting entirely."""
    import subprocess
    from aurora import ui
    repo = _init_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ui, "select", lambda *a, **k: "y")

    drafted = []
    monkeypatch.setattr("aurora.gitcommit.draft_message",
                        lambda *a, **k: drafted.append(1))

    ui._commit_cmd(None, None, "a real message")
    assert not drafted   # draft_message never called — the message was given
    out = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == "a real message"
    assert "committed" in capsys.readouterr().out


def test_commit_cmd_drafts_when_no_message_given(tmp_path, monkeypatch, capsys):
    import subprocess
    from aurora import ui
    repo = _init_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ui, "select", lambda *a, **k: "y")
    monkeypatch.setattr("aurora.gitcommit.draft_message",
                        lambda *a, **k: "drafted commit message")

    ui._commit_cmd(object(), None, "")
    out = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == "drafted commit message"


def test_commit_cmd_nothing_staged_offers_to_stage_all(tmp_path, monkeypatch, capsys):
    import subprocess
    from aurora import ui
    repo = _init_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("v2\n")           # modified, NOT staged
    (repo / "new.txt").write_text("hi\n")          # untracked
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)   # yes, stage all
    monkeypatch.setattr(ui, "select", lambda *a, **k: "y")
    monkeypatch.setattr("aurora.gitcommit.draft_message",
                        lambda *a, **k: "staged everything")

    ui._commit_cmd(object(), None, "")
    diff_out = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--stat", "--format="],
        capture_output=True, text=True, check=True).stdout
    assert "a.txt" in diff_out and "new.txt" in diff_out


def test_commit_cmd_declining_to_stage_cancels(tmp_path, monkeypatch, capsys):
    from aurora import ui
    repo = _init_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("v2\n")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)   # no, don't stage
    ui._commit_cmd(object(), None, "")
    assert "cancelled" in capsys.readouterr().out.lower()


def test_commit_cmd_choosing_no_at_the_final_confirm_does_not_commit(tmp_path, monkeypatch, capsys):
    import subprocess
    from aurora import ui
    repo = _init_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ui, "select", lambda *a, **k: "n")
    ui._commit_cmd(object(), None, "a message")
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert "a message" not in log   # never committed
    # still staged, not discarded
    assert "a.txt" in subprocess.run(
        ["git", "-C", str(repo), "diff", "--staged", "--name-only"],
        capture_output=True, text=True, check=True).stdout


def test_commit_cmd_edit_choice_lets_the_user_rewrite_the_message(tmp_path, monkeypatch):
    import subprocess
    from aurora import ui
    repo = _init_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    monkeypatch.chdir(repo)
    choices = iter(["e", "y"])   # edit once, then confirm
    monkeypatch.setattr(ui, "select", lambda *a, **k: next(choices))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "edited message")

    ui._commit_cmd(object(), None, "original message")
    out = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == "edited message"


def test_commit_cmd_refuses_an_empty_message(tmp_path, monkeypatch, capsys):
    """An empty draft (or an empty edit) must never produce an empty
    commit — the loop must ask again, not silently commit with nothing."""
    import subprocess
    from aurora import ui
    repo = _init_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    monkeypatch.chdir(repo)
    choices = iter(["y", "n"])   # first "y" with an empty message: refused;
    # loop re-prompts; second answer "n" exits the test
    monkeypatch.setattr(ui, "select", lambda *a, **k: next(choices))

    ui._commit_cmd(object(), None, "")   # no message given, no draft either
    out = capsys.readouterr().out
    assert "refusing an empty commit message" in out
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert "initial" == log.strip()   # nothing new committed


def test_commit_is_registered_in_command_dispatch():
    from aurora import ui
    assert "commit" in ui.COMMAND_INFO


def test_agentic_report_cmd_stats_choice_runs_stats_sh(tmp_path, monkeypatch, capsys):
    from aurora import ui, memory
    root = tmp_path / ".agentic_context"
    (root / "KNOWLEDGE").mkdir(parents=True)
    (root / "MEMORY").mkdir(parents=True)
    (root / "KNOWLEDGE" / "SKILL.md").write_text("x")
    (root / "MEMORY" / "SKILL.md").write_text("x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ui, "select", lambda *_a, **_k: "stats")
    monkeypatch.setattr(memory, "run_stats", lambda r: f"STATS for {r}")
    ui._agentic_report_cmd(None, None)
    assert f"STATS for {root}" in capsys.readouterr().out


def test_agentic_report_cmd_index_choice_pretty_prints_both_indexes(tmp_path, monkeypatch, capsys):
    from aurora import ui, mdrender
    # mdrender's markdown->ANSI is a no-op with colours off (non-tty, as
    # capsys makes stdout) — force it on to actually exercise the
    # pretty-printing this test is checking for
    monkeypatch.setattr(mdrender, "RESET", "\033[0m")
    monkeypatch.setattr(mdrender, "BOLD", "\033[1m")
    monkeypatch.setattr(mdrender, "CYAN", "\033[36m")
    root = tmp_path / ".agentic_context"
    (root / "KNOWLEDGE").mkdir(parents=True)
    (root / "MEMORY").mkdir(parents=True)
    (root / "KNOWLEDGE" / "SKILL.md").write_text("x")
    (root / "MEMORY" / "SKILL.md").write_text("x")
    (root / "KNOWLEDGE" / "INDEX.md").write_text("# KNOWLEDGE index\n- `a.md` — thing\n")
    (root / "MEMORY" / "INDEX.md").write_text("# MEMORY index\n- `b.md` — finding\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ui, "select", lambda *_a, **_k: "index")
    ui._agentic_report_cmd(None, None)
    out = capsys.readouterr().out
    assert "KNOWLEDGE/INDEX.md" in out and "MEMORY/INDEX.md" in out
    assert "KNOWLEDGE index" in out and "MEMORY index" in out
    # pretty-printed via mdrender, not raw markdown: the leading "- " bullet
    # marker is replaced with "• " and the raw "# " header prefix is gone
    assert "• " in out
    assert "# KNOWLEDGE index" not in out


def test_help_text_hides_agentic_report_by_default():
    from aurora import ui
    assert "/agentic_report" not in ui.help_text()
    assert "/agentic_report" not in ui.help_text(False)
    assert "/agentic_report" in ui.help_text(True)


def test_slash_completer_hides_agentic_report_without_context(tmp_path, monkeypatch):
    from aurora import ui
    from prompt_toolkit.document import Document
    monkeypatch.chdir(tmp_path)               # no context folder above cwd
    comp = ui.SlashCompleter(str(tmp_path))
    hits = list(comp.get_completions(Document("/agentic"), None))
    assert hits == []


def test_slash_completer_shows_agentic_report_with_context(tmp_path, monkeypatch):
    from aurora import ui
    from prompt_toolkit.document import Document
    root = tmp_path / ".agentic_context"
    (root / "KNOWLEDGE").mkdir(parents=True)
    (root / "MEMORY").mkdir(parents=True)
    (root / "KNOWLEDGE" / "SKILL.md").write_text("x")
    (root / "MEMORY" / "SKILL.md").write_text("x")
    monkeypatch.chdir(tmp_path)
    comp = ui.SlashCompleter(str(tmp_path))
    hits = list(comp.get_completions(Document("/agentic"), None))
    assert len(hits) == 1 and hits[0].text == "agentic_report"


def test_slash_completer_detects_context_from_cwd_not_config_dir(tmp_path, monkeypatch):
    """R90c: detection keys on the CWD, never the config's _base_dir — the
    config lives in the Aurora checkout, which has its own context folder,
    so keying on it offered /agentic_report in every project."""
    from aurora import ui
    from prompt_toolkit.document import Document
    cfg_dir = tmp_path / "checkout"
    (cfg_dir / ".agentic_context" / "KNOWLEDGE").mkdir(parents=True)
    (cfg_dir / ".agentic_context" / "MEMORY").mkdir(parents=True)
    (cfg_dir / ".agentic_context" / "KNOWLEDGE" / "SKILL.md").write_text("x")
    (cfg_dir / ".agentic_context" / "MEMORY" / "SKILL.md").write_text("x")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)                # cwd has no context folder
    comp = ui.SlashCompleter(str(cfg_dir))    # ...but the config dir does
    assert list(comp.get_completions(Document("/agentic"), None)) == []


# ── R96a: the completer is not allowed to touch the filesystem per keystroke ─
def _mk_skill(sk, name, body_lines=1):
    p = sk / f"{name}.py"
    p.write_text(f"# blurb for {name}\n" + "# filler\n" * body_lines)
    return p


def test_blurb_reads_the_head_not_the_whole_file(tmp_path, monkeypatch):
    """R96a: `_blurb` only ever inspects the first three lines, so slurping
    the file (it used `read_text()`) was pure waste on the per-keystroke
    completer path. Reading the whole file is the regression, so forbid it."""
    from aurora import skills
    from pathlib import Path
    sk = tmp_path / "skills"
    sk.mkdir()
    p = _mk_skill(sk, "big", body_lines=50_000)

    def _boom(*a, **kw):
        raise AssertionError("_blurb slurped the whole file")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert skills._blurb(p) == "blurb for big"


def test_completer_does_not_rescan_skills_every_keystroke(tmp_path, monkeypatch):
    """R96a: get_completions runs inline on the prompt_toolkit event-loop
    thread (the default get_completions_async just iterates it) and fires on
    every character with complete_while_typing. Walking the skills dir and
    opening every skill there put blocking I/O into keystroke latency."""
    from aurora import skills, ui
    from prompt_toolkit.document import Document
    sk = tmp_path / "skills"
    sk.mkdir()
    for i in range(5):
        _mk_skill(sk, f"skill{i}")

    calls = {"discover": 0, "blurb": 0}
    real_discover, real_blurb = skills.discover, skills._blurb
    monkeypatch.setattr(skills, "discover",
                        lambda *a, **k: (calls.__setitem__("discover", calls["discover"] + 1),
                                         real_discover(*a, **k))[1])
    monkeypatch.setattr(skills, "_blurb",
                        lambda p: (calls.__setitem__("blurb", calls["blurb"] + 1),
                                   real_blurb(p))[1])
    monkeypatch.chdir(tmp_path)
    comp = ui.SlashCompleter(str(tmp_path))
    # simulate typing "/skill" one character at a time
    for n in range(1, 7):
        list(comp.get_completions(Document("/skill"[:n]), None))

    assert calls["discover"] == 1, "skills dir re-walked per keystroke"
    assert calls["blurb"] == 5, "every skill re-opened per keystroke"


def test_completer_still_notices_a_newly_added_skill(tmp_path, monkeypatch):
    """R96a's cache is keyed on the skills dirs' mtimes, not frozen for the
    session — dropping a skill in must still show up in autocomplete."""
    import os
    from aurora import ui
    from prompt_toolkit.document import Document
    sk = tmp_path / "skills"
    sk.mkdir()
    _mk_skill(sk, "zeta")
    monkeypatch.chdir(tmp_path)
    comp = ui.SlashCompleter(str(tmp_path))
    names = {c.text for c in comp.get_completions(Document("/ze"), None)}
    assert names == {"zeta"}

    _mk_skill(sk, "zebra")
    # a human drops a skill in seconds later, never inside one mtime tick —
    # make the elapsed time explicit so the test can't race the clock
    st = sk.stat()
    os.utime(sk, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    names = {c.text for c in comp.get_completions(Document("/ze"), None)}
    assert names == {"zeta", "zebra"}


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


def test_failed_turn_does_not_relog_the_previous_answer(tmp_path, monkeypatch):
    """R95e: a turn that produces nothing pops its dangling user message,
    which leaves messages[-1] on the PREVIOUS turn's assistant reply. Logging
    that as a fresh `assistant` event re-records an old answer — /cost (R92)
    then counts a turn that never happened."""
    from aurora.providers.base import ProviderError
    from aurora import session as sessionmod

    e = _mk_engine(tmp_path, monkeypatch)
    e.redact_secrets = False

    class _FE:
        on_text = staticmethod(lambda t: None)
        on_tool_start = staticmethod(lambda n, a: None)
        on_tool_result = staticmethod(lambda n, o: None)
        approve = staticmethod(lambda *a: "y")
        ask_continue = staticmethod(lambda i: True)
        notify = staticmethod(lambda m: None)
        cancelled = staticmethod(lambda: False)

    class _Prov:
        api_key, extra_body, on_think, cache_prompt = "k", {}, None, False

        def __init__(self, fail):
            self.fail = fail

        def turn(self, *a, **k):
            if self.fail:
                raise ProviderError("boom timed out")
            return TurnResult(text="REAL ANSWER", stop_reason="end",
                              input_tokens=10, output_tokens=5)

        def assistant_message(self, r):
            return {"role": "assistant", "content": r.text}

        def cost(self, m, i, o):
            return 0.0

    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: _Prov(False))
    e.send("q1", _FE())
    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: _Prov(True))
    e.send("q2", _FE())

    logged = [r for r in e.session.iter_records() if r["event"] == "assistant"]
    assert [r["text"] for r in logged] == ["REAL ANSWER"]
    usage = sessionmod.usage_by_model(e.session.id)
    assert sum(v["turns"] for v in usage.values()) == 1
    # the failed prompt is still popped, so history never stacks two users
    assert [m["role"] for m in e.messages] == ["user", "assistant"]


def test_context_stats_never_blocks_on_a_slow_backend(tmp_path, monkeypatch):
    """R95i: status() calls context_stats() on the UI event-loop thread every
    render. A local backend that is down made the limit lookup a ~6s probe,
    freezing the whole app each time the 120s cache expired."""
    started = threading.Event()

    class _SlowProv:
        api_key = "k"

        def context_limit(self, model):
            started.set()
            time.sleep(3)              # a hung /props probe
            return 65_536

        def static_context_limit(self, model):
            return 8_000

        def has_pricing(self, model):
            return False

    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: _SlowProv())

    t0 = time.monotonic()
    s = e.context_stats()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"context_stats blocked for {elapsed:.1f}s"
    assert s.limit == 8_000            # the offline answer, served instantly
    assert started.wait(2)             # the live lookup runs, just not here

    # repeated renders while it is in flight stay fast and spawn no new probes
    for _ in range(5):
        assert e.context_stats().limit == 8_000
    assert threading.active_count() < 20

    # once it lands, the live value takes over
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and e.context_stats().limit != 65_536:
        time.sleep(0.05)
    assert e.context_stats().limit == 65_536


# ── R96i: _limit_pending's check-then-add must be atomic ──────────────────
class _SlowContainsSet(set):
    """Widens the `in` check's window so a genuine check-then-add race is
    forced open deterministically instead of relying on the scheduler to
    happen to interleave two threads inside a few bytecodes — CPython's GIL
    makes the real race rare enough that a plain concurrent-threads test
    passed even on the unfixed code across 8 runs."""

    def __contains__(self, item):
        result = super().__contains__(item)
        time.sleep(0.05)
        return result


def test_context_limit_refresh_spawns_only_one_probe_under_concurrency(tmp_path, monkeypatch):
    """R96i: `if key not in pending: pending.add(key)` is two operations —
    two near-simultaneous callers (a UI render racing the classic footer,
    say) could both observe "not pending" before either added it, spawning
    two probe threads for the same key. The lock must serialize the check
    AND the add as one unit; widening just the `in` check (not the add)
    still exposes the race if the two aren't held under the same lock."""
    probe_starts = []
    probe_lock = threading.Lock()

    class _SlowProv:
        api_key = "k"

        def context_limit(self, model):
            with probe_lock:
                probe_starts.append(1)
            # stay "in flight" for the whole test — otherwise the first
            # probe legitimately finishes and discard()s the key before a
            # later checker arrives, which is correct behaviour (a NEW probe
            # for the same key after the old one completed), not the race
            # this test targets
            time.sleep(1)
            return 65_536

        def static_context_limit(self, model):
            return 8_000

        def has_pricing(self, model):
            return False

    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: _SlowProv())
    e._limit_pending = _SlowContainsSet()

    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        e.context_stats()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=2)

    time.sleep(0.2)
    assert len(probe_starts) == 1, \
        f"{len(probe_starts)} probe threads spawned for one key under a forced race"


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


# ── R90: deep-dive batch 3 ─────────────────────────────────────────────────
def test_read_file_line_range(tmp_path):
    f = tmp_path / "lines.txt"
    f.write_text("".join(f"line{i}\n" for i in range(1, 21)))
    out = tools.read_file(str(f), offset=5, limit=3)
    assert "line5\nline6\nline7" in out
    assert "line4" not in out and "line8" not in out
    assert out.startswith("[lines 5-7, more follow]")
    tail = tools.read_file(str(f), offset=19, limit=5)     # runs to EOF
    assert tail.startswith("[lines 19-20 of 20]")


def test_read_file_without_range_is_unchanged(tmp_path):
    f = tmp_path / "lines.txt"
    f.write_text("a\nb\nc\n")
    assert tools.read_file(str(f)) == "a\nb\nc\n"


def test_read_file_range_past_eof_is_a_clean_message(tmp_path):
    f = tmp_path / "lines.txt"
    f.write_text("a\nb\n")
    assert "fewer than" in tools.read_file(str(f), offset=99, limit=2)


def test_grep_uses_extended_regex(tmp_path):
    """BRE would treat (alpha|beta) as literal text and silently return no
    matches — the failure mode that reads as 'the code isn't there' (R90b)."""
    (tmp_path / "a.py").write_text("def alpha():\n    pass\n")
    (tmp_path / "b.py").write_text("def beta():\n    pass\n")
    out = tools.grep("def (alpha|beta)", str(tmp_path))
    assert "alpha" in out and "beta" in out


def test_edit_file_replace_all(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("old\nold\nold\n")
    assert "3 times" in tools.edit_file(str(f), "old", "new")   # still guarded
    out = tools.edit_file(str(f), "old", "new", replace_all=True)
    assert f.read_text() == "new\nnew\nnew\n" and "3 occurrences" in out


# ── R97: apply_patch ────────────────────────────────────────────────────
def test_apply_patch_applies_multiple_hunks(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\ne\n")
    diff = ("@@ -1,2 +1,2 @@\n"
            " a\n"
            "-b\n"
            "+B\n"
            "@@ -4,2 +4,2 @@\n"
            " d\n"
            "-e\n"
            "+E\n")
    out = tools.apply_patch(str(f), diff)
    assert "applied 2 hunk" in out
    assert f.read_text() == "a\nB\nc\nd\nE\n"


def test_apply_patch_no_such_file(tmp_path):
    out = tools.apply_patch(str(tmp_path / "nope.py"),
                            "@@ -1,1 +1,1 @@\n-x\n+y\n")
    assert "no such file" in out


def test_apply_patch_surfaces_context_not_found_and_touches_nothing(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("original\n")
    out = tools.apply_patch(str(f), "@@ -1,1 +1,1 @@\n-nonexistent\n+y\n")
    assert "error" in out.lower() and "context not found" in out
    assert f.read_text() == "original\n"   # untouched — all-or-nothing


def test_apply_patch_all_or_nothing_across_hunks(tmp_path):
    """The first hunk is valid; the second isn't. Neither may land."""
    f = tmp_path / "x.py"
    f.write_text("a\nb\n")
    diff = ("@@ -1,1 +1,1 @@\n-a\n+A\n"
            "@@ -1,1 +1,1 @@\n-nonexistent\n+X\n")
    out = tools.apply_patch(str(f), diff)
    assert "error" in out.lower()
    assert f.read_text() == "a\nb\n"   # the valid first hunk did NOT land either


def test_apply_patch_reports_a_true_no_op(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("same\n")
    out = tools.apply_patch(str(f), "@@ -1,1 +1,1 @@\n same\n")
    assert "no-op" in out.lower() or "no changes" in out.lower()
    assert f.read_text() == "same\n"   # never touched on disk for a no-op


def test_apply_patch_bad_diff_reports_parse_error(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x\n")
    out = tools.apply_patch(str(f), "not a diff at all")
    assert "error" in out.lower()


def test_apply_patch_is_registered_and_needs_approval():
    assert "apply_patch" in tools.RUNNERS
    assert "apply_patch" in tools.NEEDS_APPROVAL
    assert "apply_patch" not in tools.PARALLEL_SAFE   # mutates — never concurrent
    names = [s["name"] for s in tools.SPEC]
    assert "apply_patch" in names


def test_apply_patch_allowlist_round_trips(tmp_path, monkeypatch):
    """R97: apply_patch needs its own allowlist bucket in load()'s
    setdefault — otherwise add_rule("apply_patch", ...) KeyErrors the first
    time a user picks 'always allow' on one."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    args = {"path": str(tmp_path / "x.py"), "diff": "@@ -1,1 +1,1 @@\n-a\n+b\n"}
    assert not approve.is_allowed("apply_patch", args)
    rule = approve.add_rule("apply_patch", args)
    assert rule == str(tmp_path / "x.py")
    assert approve.is_allowed("apply_patch", args)


# ── R97: apply_patch's approval preview ─────────────────────────────────
def test_apply_patch_preview_shows_the_real_computed_diff(tmp_path):
    """R97, same principle as R95a: the preview must show what the patch
    will ACTUALLY produce (computed by really applying it here), not the
    model's raw submitted diff text — the two could disagree."""
    f = tmp_path / "x.py"
    f.write_text("one\ntwo\nthree\n")
    diff = "@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
    preview = approve.diff_preview("apply_patch", {"path": str(f), "diff": diff})
    assert "-two" in preview and "+TWO" in preview
    assert f.read_text() == "one\ntwo\nthree\n"   # preview must not write


def test_apply_patch_preview_surfaces_a_bad_patch_before_approval(tmp_path):
    """A patch that would fail to apply must be visible AT the approval
    prompt, not just discovered after the user already said yes."""
    f = tmp_path / "x.py"
    f.write_text("original\n")
    preview = approve.diff_preview(
        "apply_patch", {"path": str(f), "diff": "@@ -1,1 +1,1 @@\n-missing\n+y\n"})
    assert "context not found" in preview or "PatchError" in preview


def test_apply_patch_preview_missing_file():
    preview = approve.diff_preview(
        "apply_patch", {"path": "/no/such/file.py", "diff": "@@ -1,1 +1,1 @@\n-a\n+b\n"})
    assert "no such file" in preview.lower() or "error" in preview.lower()


def test_run_command_honours_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    assert "marker.txt" in tools.run_command("ls", cwd=str(tmp_path))
    assert "no such directory" in tools.run_command("ls", cwd=str(tmp_path / "nope"))


# ── R100: wait_until ────────────────────────────────────────────────────
def test_wait_until_succeeds_on_first_attempt_when_already_true(tmp_path):
    marker = tmp_path / "ready"
    marker.write_text("x")
    out = tools.wait_until(f"test -f {marker}", interval=0.05, timeout=2)
    assert "succeeded after 1 attempt" in out


def test_wait_until_polls_until_a_condition_becomes_true(tmp_path):
    """The condition is false at first, becomes true partway through — the
    tool must keep polling rather than giving up on the first failure."""
    marker = tmp_path / "ready"
    out = tools.wait_until(
        f"test -f {marker} || (sleep 0.1 && touch {marker})",
        interval=0.05, timeout=3)
    assert "succeeded" in out
    assert marker.is_file()


def test_wait_until_gives_up_after_timeout(tmp_path):
    out = tools.wait_until("false", interval=0.05, timeout=0.3)
    assert "gave up" in out and "exit 1" in out


def test_wait_until_reports_a_command_that_times_out_mid_attempt(monkeypatch):
    """A single attempt that itself times out (code is None from
    _run_command_once) must be reported distinctly from a plain nonzero
    exit — 'timed out mid-command', not a fabricated exit code."""
    monkeypatch.setattr(tools, "_run_command_once",
                        lambda command, workdir: ("partial output", None))
    out = tools.wait_until("some-slow-command", interval=0.05, timeout=0.2)
    assert "timed out mid-command" in out


def test_wait_until_honours_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    out = tools.wait_until("test -f marker.txt", cwd=str(tmp_path),
                           interval=0.05, timeout=2)
    assert "succeeded" in out
    assert "no such directory" in tools.wait_until(
        "true", cwd=str(tmp_path / "nope"), interval=0.05, timeout=1)


def test_wait_until_timeout_is_bounded(monkeypatch):
    """A model passing an absurd timeout must not turn this into an
    unbounded background job — same spirit as COMMAND_TIMEOUT's own ceiling.
    A fake, fast-forwarding monotonic clock exercises the real 300s clamp
    without the test itself taking anywhere near 300 real seconds — patching
    time.sleep alone wouldn't do it, since monotonic() reflects the real
    wall clock regardless of whether anything actually sleeps."""
    fake_now = [0.0]

    def fake_monotonic():
        fake_now[0] += 50.0   # each check "advances" 50 fake seconds
        return fake_now[0]

    monkeypatch.setattr("time.monotonic", fake_monotonic)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(tools, "_run_command_once",
                        lambda command, workdir: ("", 1))
    out = tools.wait_until("false", interval=0.01, timeout=10_000_000)
    # clamped to 300s: at +50 fake-seconds per check, this must give up
    # within single-digit attempts, not spin for a very long time
    assert "gave up" in out


def test_wait_until_is_registered_and_needs_approval():
    assert "wait_until" in tools.RUNNERS
    assert "wait_until" in tools.NEEDS_APPROVAL
    assert "wait_until" not in tools.PARALLEL_SAFE
    names = [s["name"] for s in tools.SPEC]
    assert "wait_until" in names


def test_wait_until_allowlist_is_separate_from_run_command(tmp_path, monkeypatch):
    """R100: an 'always allow' on run_command must never silently cover
    wait_until — different risk shape (repeated execution), own bucket."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    args = {"command": "npm run dev"}
    approve.add_rule("run_command", args)
    assert approve.is_allowed("run_command", args)
    assert not approve.is_allowed("wait_until", args)   # NOT covered

    approve.add_rule("wait_until", args)
    assert approve.is_allowed("wait_until", args)


def test_read_file_range_stops_at_the_byte_cap(tmp_path):
    """R95f: `offset` with no `limit` used to accumulate every remaining line
    before truncating — the slurp the streaming loop exists to avoid."""
    f = tmp_path / "big.txt"
    line = "x" * 999 + "\n"
    f.write_text(line * 400)                      # ~400KB, over MAX_READ_BYTES
    out = tools.read_file(str(f), offset=1)
    assert len(out) < tools.MAX_READ_BYTES + 2000
    assert "more follow" in out                   # honest about stopping early


def test_file_allowlist_matches_across_path_spellings(tmp_path, monkeypatch):
    """R95g: run_command rules normalize their tokens so spelling variants
    match one rule; file rules did raw fnmatch, so ~/x.py and its expansion
    were two rules and 'always allow' re-prompted on the other spelling."""
    monkeypatch.setenv("HOME", str(tmp_path))
    approve.save({"run_command": [], "write_file": [], "edit_file": []})
    rule = approve.add_rule("write_file", {"path": "~/notes.md"})
    assert rule == str(tmp_path / "notes.md")     # stored expanded
    data = approve.load()
    for spelling in ("~/notes.md", str(tmp_path / "notes.md")):
        assert approve.is_allowed("write_file", {"path": spelling}, data), spelling
    assert not approve.is_allowed("write_file", {"path": "~/other.md"}, data)


def test_legacy_raw_file_rule_still_matches(tmp_path, monkeypatch):
    """A rule stored before R95g is raw (`~/x.py`); normalizing both sides
    keeps it working instead of silently dropping it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    approve.save({"run_command": [], "write_file": ["~/legacy.md"],
                  "edit_file": []})
    assert approve.is_allowed("write_file",
                              {"path": str(tmp_path / "legacy.md")})


def test_diff_preview_matches_replace_all(tmp_path):
    """R95a: the approval diff must show what the edit WILL do. With
    replace_all the preview used a fixed count of 1, so the human approved a
    one-line diff while every occurrence was about to change."""
    f = tmp_path / "x.py"
    f.write_text("foo=1\nfoo=2\nfoo=3\n")
    args = {"path": str(f), "old": "foo", "new": "bar"}

    def changed(diff):
        return sum(1 for line in diff.splitlines()
                   if line.startswith("-") and not line.startswith("---"))

    assert changed(approve.diff_preview("edit_file", args)) == 1
    assert changed(approve.diff_preview(
        "edit_file", {**args, "replace_all": True})) == 3
    # and the preview is the truth: the real edit changes exactly that many
    tools.edit_file(**{**args, "replace_all": True})
    assert f.read_text() == "bar=1\nbar=2\nbar=3\n"


def test_grep_reports_errors_instead_of_no_matches(tmp_path):
    """R95b: grep exits 1 for 'no match' but >=2 for an ERROR. Reporting an
    invalid regex as '[no matches]' is the R90b failure mode — the model
    concludes the code doesn't exist rather than fixing its pattern."""
    (tmp_path / "a.py").write_text("hello\n")
    bad = tools.grep("(unclosed", str(tmp_path))
    assert "error" in bad.lower() and "no matches" not in bad
    missing = tools.grep("x", str(tmp_path / "nope"))
    assert "error" in missing.lower() and "no matches" not in missing
    # a genuine miss is still a plain no-match, and real hits are unaffected
    assert tools.grep("zzz_absent_zzz", str(tmp_path)) == "[no matches]"
    assert "hello" in tools.grep("hello", str(tmp_path))


# ── R96m: grep must bound the PRODUCER, not slurp then truncate ───────────
def test_grep_kills_the_process_once_the_cap_is_reached(tmp_path, monkeypatch):
    """R96m: the old implementation used subprocess.run(capture_output=True),
    which buffers grep's ENTIRE stdout before [:MAX_READ_BYTES] ever runs —
    an over-broad pattern over a large tree can exhaust memory within the
    30s timeout. Both the old and new code produce the same TRUNCATED
    STRING at the end, so the only way to prove the producer is actually
    bounded (not just the final string) is to check that grep was KILLED
    once the cap was reached rather than left to run to completion: a
    killed process gets a negative returncode (SIGKILL == -9 on POSIX);
    finishing normally on its own gives 0."""
    import subprocess as sp
    monkeypatch.setattr(tools, "MAX_READ_BYTES", 1000)   # small, fast to hit
    big = tmp_path / "big.txt"
    # far more matching bytes than the 1000-byte cap — each line is ~20
    # bytes, so 1,000,000 lines is ~20MB of matches; grep alone finishes
    # this comfortably inside the 30s timeout, so "ran to completion" and
    # "was killed early" are genuinely distinguishable outcomes here
    with open(big, "w") as f:
        for i in range(1_000_000):
            f.write(f"needle line {i:07d}\n")

    real_popen = sp.Popen
    procs = []

    def tracking_popen(*a, **kw):
        p = real_popen(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(sp, "Popen", tracking_popen)
    out = tools.grep("needle", str(tmp_path))

    assert "needle" in out
    assert "truncated" in out
    assert len(out) < 5000   # nowhere near the full ~20MB of real matches
    assert procs, "grep didn't go through subprocess.Popen"
    procs[0].wait(timeout=2)
    assert procs[0].returncode is not None and procs[0].returncode < 0, \
        (f"returncode={procs[0].returncode} — grep ran to completion "
         f"instead of being killed once the cap was reached")


def test_grep_reports_timeout_and_reaps_the_process(tmp_path, monkeypatch):
    """R96m's timeout branch: the incremental read loop must still honour a
    real deadline (not just the truncation cap), report it plainly, and
    reap the process rather than leaving it running. Forces the branch by
    making select.select report 'never ready' — the underlying grep process
    is real and harmless, but the read loop must never see its output."""
    import select as select_mod
    import subprocess as sp

    (tmp_path / "a.py").write_text("hello\n")
    monkeypatch.setattr(tools, "GREP_TIMEOUT", 0.05)
    monkeypatch.setattr(select_mod, "select", lambda *a, **k: ([], [], []))

    real_popen = sp.Popen
    procs = []

    def tracking_popen(*a, **kw):
        p = real_popen(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(sp, "Popen", tracking_popen)
    out = tools.grep("hello", str(tmp_path))

    assert "error" in out.lower() and "timeout" in out.lower()
    assert procs, "grep didn't go through subprocess.Popen"
    procs[0].wait(timeout=2)
    assert procs[0].returncode is not None, "process was left unreaped"


def test_grep_still_finds_real_matches_under_the_cap(tmp_path):
    """Correctness check alongside the truncation test — an ordinary search
    well under MAX_READ_BYTES must return every match, complete and
    untruncated, exactly as before."""
    (tmp_path / "a.py").write_text("needle one\n")
    (tmp_path / "b.py").write_text("needle two\n")
    out = tools.grep("needle", str(tmp_path))
    assert "needle one" in out and "needle two" in out
    assert "truncated" not in out


def test_grep_reaps_the_child_process_on_truncation(tmp_path, monkeypatch):
    """R96m: killing the process early must not leave a zombie/orphan behind
    — the same durability bar R95c set for run_command's timeout path."""
    import subprocess as sp
    monkeypatch.setattr(tools, "MAX_READ_BYTES", 500)
    big = tmp_path / "big.txt"
    with open(big, "w") as f:
        for i in range(200_000):
            f.write(f"needle {i:07d}\n")
    real_popen = sp.Popen
    procs = []

    def tracking_popen(*a, **kw):
        p = real_popen(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(sp, "Popen", tracking_popen)
    tools.grep("needle", str(tmp_path))
    assert procs, "grep didn't use subprocess.Popen"
    proc = procs[0]
    proc.wait(timeout=2)   # must already be reaped, not left running
    assert proc.returncode is not None


def test_run_command_timeout_kills_the_whole_process_group(tmp_path):
    """R95c: subprocess.run(shell=True, timeout=…) kills only the shell —
    children it spawned survive, reparented to init, and keep running for the
    rest of the session. Aurora must kill the whole group."""
    def timed_out(command):
        prev = tools.COMMAND_TIMEOUT
        tools.set_command_timeout(1)
        try:
            return tools.run_command(command)
        finally:
            tools.set_command_timeout(prev)

    def assert_dead(pid_file):
        time.sleep(0.2)
        pid = int(pid_file.read_text().strip())
        with pytest.raises(ProcessLookupError):   # signal 0 = "does it exist?"
            os.kill(pid, 0)

    # the shell outlives its child (`wait`)
    a = tmp_path / "a.pid"
    out = timed_out(f"sleep 60 & echo $! > {a}; echo started; wait")
    assert "timeout after 1s" in out
    assert "started" in out          # partial output is kept, not discarded
    assert_dead(a)

    # the shell EXITS and leaves the grandchild holding the pipe. This is the
    # case that matters: communicate() blocks the full timeout on a shell
    # that is already gone, and looking the group up only then is too late.
    b = tmp_path / "b.pid"
    assert "timeout after 1s" in timed_out(f"(sleep 60 & echo $! > {b}); wait")
    assert_dead(b)


def test_gauge_uses_last_reply_not_summed_output(tmp_path, monkeypatch):
    """A multi-tool turn re-sends the whole context each round; every earlier
    reply is already inside the next round's PROMPT, so summing outputs into
    the gauge double-counts them (R90d). Cost keeps the sums."""
    approve.save({"run_command": [], "write_file": ["*"], "edit_file": []})
    f = tmp_path / "o.txt"
    prov = FakeProvider([
        TurnResult(text="", tool_calls=[ToolCall("1", "write_file",
                   {"path": str(f), "content": "hi"})], stop_reason="tool_use",
                   input_tokens=100, output_tokens=30),
        TurnResult(text="done", stop_reason="end",
                   input_tokens=400, output_tokens=50),
    ])
    msgs = [{"role": "user", "content": "go"}]
    t = agent.run_turn(prov, "m", msgs, "sys", _cb(), 5, True, False)
    assert t.output_tokens == 80        # billed: every round
    assert t.last_output_tokens == 50   # occupying context: the last one
    assert t.billed_input == 500
    assert t.input_tokens == 400


def test_context_stats_with_no_model_builds_no_provider(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    e.current = {}
    s = e.context_stats()
    assert s.limit == 0 and s.model == "" and e._provider is None


def test_resume_restores_the_context_gauge(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    e.session.log("user", text="x" * 400)
    e.session.log("assistant", text="y" * 400)
    e2 = _mk_engine(tmp_path, monkeypatch)
    assert e2.resume_from(e.session.id) == 2
    assert e2._used > 0     # not 0 until the first new turn (R90g)


def test_resume_gauge_matches_exact_char_count(tmp_path, monkeypatch):
    """R96k: the estimate sums each message's length instead of joining the
    whole history into one string first — same answer (a bare "".join adds
    no separator chars), without the transient full-history copy. Pins the
    exact value so a future change can't silently drift the estimate."""
    e = _mk_engine(tmp_path, monkeypatch)
    e.session.log("user", text="a" * 123)
    e.session.log("assistant", text="b" * 77)
    e.session.log("user", text="c" * 50)
    e2 = _mk_engine(tmp_path, monkeypatch)
    assert e2.resume_from(e.session.id) == 3
    assert e2._used == (123 + 77 + 50) // 4


# ── R91: prompt caching ────────────────────────────────────────────────────
def test_cache_breakpoint_marks_a_big_system_prompt():
    from aurora.providers.openai_compat import _system_message, _CACHE_MIN_CHARS
    big = "x" * _CACHE_MIN_CHARS
    msg = _system_message(big, cache=True)
    assert msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msg["content"][0]["text"] == big


def test_cache_breakpoint_skipped_when_off_or_too_small():
    from aurora.providers.openai_compat import _system_message, _CACHE_MIN_CHARS
    big = "x" * _CACHE_MIN_CHARS
    # off → plain string, byte-identical to the pre-R91 shape
    assert _system_message(big, cache=False) == {"role": "system", "content": big}
    # too small to be worth a cache WRITE (which costs more than a plain read)
    small = "x" * (_CACHE_MIN_CHARS - 1)
    assert _system_message(small, cache=True) == {"role": "system", "content": small}


def test_cache_enabled_defaults_off_for_local_on_for_remote(tmp_path, monkeypatch):
    e = _mk_engine(tmp_path, monkeypatch)
    assert e.cache_enabled({"model": "some-remote", "provider": "local"}) is True
    # llama.cpp keeps its own KV prefix — nothing to bill, nothing to mark
    assert e.cache_enabled({"model": "local", "provider": "local"}) is False
    # a per-model flag overrides either way, exactly like `tools:`
    assert e.cache_enabled({"model": "local", "cache": True}) is True
    assert e.cache_enabled({"model": "remote", "cache": False}) is False
    e.prompt_cache = False        # the global switch beats both
    assert e.cache_enabled({"model": "remote", "cache": True}) is False


def test_turn_sums_cached_tokens_across_iterations(tmp_path):
    approve.save({"run_command": [], "write_file": ["*"], "edit_file": []})
    f = tmp_path / "o.txt"
    r1 = TurnResult(text="", tool_calls=[ToolCall("1", "write_file",
                    {"path": str(f), "content": "hi"})], stop_reason="tool_use",
                    input_tokens=100, output_tokens=10)
    r1.cached_input_tokens = 80
    r2 = TurnResult(text="done", stop_reason="end",
                    input_tokens=200, output_tokens=20)
    r2.cached_input_tokens = 150
    t = agent.run_turn(FakeProvider([r1, r2]), "m",
                       [{"role": "user", "content": "go"}], "sys", _cb(),
                       5, True, False)
    assert t.cached_input == 230
    assert t.billed_input == 300   # NOT reduced: the estimate stays an upper bound


# ── R92: /cost ─────────────────────────────────────────────────────────────
def test_usage_by_model_reads_the_session_log(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora import session as sessions
    s = sessions.Session("costtest01")
    s.log("assistant", model="m-a", input_tokens=100, billed_input=250,
          output_tokens=40, cached_input=90)
    s.log("assistant", model="m-a", input_tokens=50, output_tokens=10)  # no billed_input
    s.log("assistant", model="m-b", input_tokens=10, billed_input=10, output_tokens=5)
    s.log("user", text="ignored")
    rows = sessions.usage_by_model("costtest01")
    assert rows["m-a"] == {"turns": 2, "input": 150, "billed": 300,
                           "output": 50, "cached": 90}
    assert rows["m-b"]["turns"] == 1
    assert sessions.usage_all_sessions()["m-a"]["billed"] == 300


# ── R96e: iter_records(events=...) must not parse what it filters out ──────
def test_iter_records_event_filter_skips_json_parse(tmp_path, monkeypatch):
    """R96e: usage_by_model only wants `assistant` records, but the log is
    dominated by `tool` records (one per tool result, each carrying up to 4KB
    of output — see Engine.send). json.loads-ing every line just to check
    `event` and discard most of them was most of /cost's cost. The filtered
    substring check must reject a non-matching line WITHOUT ever calling
    json.loads on it."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora import session as sessions
    s = sessions.Session("filtertest01")
    s.log("tool", name="read_file", output="x" * 4000)
    s.log("assistant", model="m", input_tokens=1, output_tokens=1)
    s.log("tool", name="grep", output="y" * 4000)

    real_loads = sessions.json.loads
    calls = {"n": 0}

    def counting_loads(s_):
        calls["n"] += 1
        return real_loads(s_)

    monkeypatch.setattr(sessions.json, "loads", counting_loads)
    recs = list(s.iter_records(events={"assistant"}))
    assert len(recs) == 1 and recs[0]["event"] == "assistant"
    assert calls["n"] == 1, \
        f"json.loads called {calls['n']} times filtering 3 lines to 1 match"


def test_iter_records_event_filter_matches_unfiltered_result(tmp_path, monkeypatch):
    """The filtered path must return exactly the records the unfiltered path
    would, minus the excluded events — never more, never fewer."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora import session as sessions
    s = sessions.Session("filtertest02")
    s.log("tool", name="read_file", output="out")
    s.log("assistant", model="m1", input_tokens=1, output_tokens=1)
    s.log("user", text="hi")
    s.log("assistant", model="m2", input_tokens=2, output_tokens=2)

    unfiltered = [r for r in s.iter_records() if r.get("event") == "assistant"]
    filtered = list(s.iter_records(events={"assistant"}))
    assert filtered == unfiltered
    assert [r["model"] for r in filtered] == ["m1", "m2"]


def test_usage_by_model_ignores_a_field_that_looks_like_the_event_marker(tmp_path, monkeypatch):
    """The substring pre-filter must never cause a false NEGATIVE — a record
    whose event genuinely is 'assistant' must always survive even if other
    fields contain text that could confuse a naive filter."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora import session as sessions
    s = sessions.Session("filtertest03")
    # a tool result whose OUTPUT happens to contain the literal marker text —
    # must not be mistaken for a real assistant record
    s.log("tool", name="grep", output='hits: "event": "assistant" in some file')
    s.log("assistant", model="m", input_tokens=5, output_tokens=5)
    rows = sessions.usage_by_model("filtertest03")
    assert rows["m"]["turns"] == 1


def test_cost_command_prices_known_models_and_flags_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora import ui
    from aurora.providers import openai_compat
    monkeypatch.setitem(openai_compat.REMOTE_CONTEXT_LIMITS, "priced-model",
                        {"model": "priced-model", "price_in_per_mtok": 1.0,
                         "price_out_per_mtok": 10.0})
    e = _mk_engine(tmp_path, monkeypatch)
    e.session.log("assistant", model="priced-model", billed_input=1_000_000,
                  output_tokens=100_000, cached_input=500_000)
    e.session.log("assistant", model="local", billed_input=999, output_tokens=1)
    out = ui._cost_report(e, "")
    assert "priced-model" in out and "local" in out
    assert "$2" in out              # 1M in @$1 + 100k out @$10 = $2.00
    assert "no price" in out        # local has none — never a misleading $0.00
    assert "cached" in out
    assert "nothing logged" in ui._cost_report(_mk_engine(tmp_path, monkeypatch), "")


# ── R93: the todo tool ─────────────────────────────────────────────────────
def test_todo_write_replaces_the_list_and_renders():
    from aurora import todo
    todo.clear()
    out = todo.todo_write([{"task": "read the spec", "status": "done"},
                           {"task": "fix the bug", "status": "in_progress"},
                           {"task": "add tests"}])
    assert "[x] read the spec" in out and "[~] fix the bug" in out
    assert "[ ] add tests" in out and "(1/3 done)" in out
    todo.todo_write([{"task": "only this"}])       # wholesale replace
    assert len(todo.items()) == 1
    assert todo.render() == "[ ] only this\n(0/1 done)"
    todo.clear()


def test_todo_write_tolerates_sloppy_input():
    from aurora import todo
    todo.clear()
    assert todo.todo_write("just a string").startswith("[ ] just a string")
    assert todo.todo_write([{"task": "x", "status": "nonsense"}]).startswith("[ ]")
    assert "error" in todo.todo_write(None)
    assert "cleared" in todo.todo_write([])
    todo.clear()


def test_todo_tool_is_offered_and_runnable_and_clears_with_history(tmp_path, monkeypatch):
    from aurora import todo
    assert any(s["name"] == "todo_write" for s in tools.specs(False))
    assert "[ ] step one" in tools.run_tool("todo_write", {"todos": ["step one"]})
    e = _mk_engine(tmp_path, monkeypatch)
    e.clear()                                   # the list belongs to the conversation
    assert todo.items() == []
    tools.set_todo_enabled(False)
    assert not any(s["name"] == "todo_write" for s in tools.specs(False))
    tools.set_todo_enabled(True)


# ── R94: parallel read-only tools ──────────────────────────────────────────
def test_parallel_batch_runs_read_only_calls_concurrently(monkeypatch):
    import time
    from aurora import tools as t
    calls = [(i, "read_file", {"path": f"/nope/{i}"}) for i in range(4)]

    def slow(name, args):
        time.sleep(0.2)
        return f"out-{args['path']}"

    monkeypatch.setattr(t, "run_tool", slow)
    t0 = time.monotonic()
    got = t.run_tools_parallel(calls)
    elapsed = time.monotonic() - t0
    assert got == {i: f"out-/nope/{i}" for i in range(4)}
    assert elapsed < 0.6, f"ran serially ({elapsed:.2f}s for 4×0.2s)"


def test_agent_parallelizes_reads_but_keeps_order_and_gates(tmp_path):
    """Reads run concurrently; everything the user sees — tool starts,
    approvals, results, history — stays in the model's original order."""
    approve.save({"run_command": [], "write_file": [], "edit_file": []})
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("AAA")
    b.write_text("BBB")
    target = tmp_path / "out.txt"
    prov = FakeProvider([
        TurnResult(text="", stop_reason="tool_use", tool_calls=[
            ToolCall("1", "read_file", {"path": str(a)}),
            ToolCall("2", "write_file", {"path": str(target), "content": "hi"}),
            ToolCall("3", "read_file", {"path": str(b)}),
        ]),
        TurnResult(text="done", stop_reason="end"),
    ])
    log = []
    agent.run_turn(prov, "m", [{"role": "user", "content": "go"}], "sys",
                   _cb(approve_ans="y", log=log), 5, True, False)
    results = [e for e in log if e[0] == "result"]
    assert [e[2] for e in results][:3] == ["AAA", "[wrote 2 bytes to %s]" % target, "BBB"]
    starts = [e[1] for e in log if e[0] == "start"]
    assert starts.count("read_file") == 2 and starts.count("write_file") == 1
    assert target.read_text() == "hi"   # the gated tool still ran through approval


def test_a_mutating_ungated_tool_is_never_parallelized():
    """todo_write needs no approval but rewrites shared state — the parallel
    set is an explicit allowlist, not "anything outside NEEDS_APPROVAL"."""
    assert "todo_write" not in tools.PARALLEL_SAFE
    assert tools.PARALLEL_SAFE.isdisjoint(tools.NEEDS_APPROVAL)


def test_parallel_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CFG + "runtime: {parallel_tools: false, todo_tool: false}\n")
    from aurora.engine import Engine
    Engine(str(cfg))
    assert tools.PARALLEL_ENABLED is False and tools.TODO_ENABLED is False
    tools.set_parallel_tools(True)
    tools.set_todo_enabled(True)


def test_turn_payload_carries_the_cache_breakpoint_and_reads_cached_usage(monkeypatch):
    """End-to-end through the real turn(): the wire payload must carry the
    cache_control marker, and usage.prompt_tokens_details.cached_tokens must
    come back on the TurnResult (R91)."""
    import contextlib
    from aurora.providers import openai_compat as oc
    from aurora.providers.openai_compat import _CACHE_MIN_CHARS
    prov = oc.OpenAICompatProvider("x", {"base_url": "http://127.0.0.1:9"}, 5)
    monkeypatch.setattr(prov, "pick_endpoint", lambda cache_ok=True: prov.base_url)
    seen = {}

    class _Client:
        def stream(self, method, url, headers=None, json=None):
            seen["payload"] = json     # capture the real wire payload
            return contextlib.nullcontext()

    monkeypatch.setattr(prov, "_client_for", lambda base: _Client())

    def fake_sse(open_stream, cancel, poll=0.15):
        open_stream()          # materializes the payload above
        yield ("status", 200, None)
        yield ("line", 'data: {"usage":{"prompt_tokens":900,'
                       '"completion_tokens":10,'
                       '"prompt_tokens_details":{"cached_tokens":800}},'
                       '"choices":[{"delta":{"content":"hi"}}]}', None)
        yield ("line", "data: [DONE]", None)

    monkeypatch.setattr(oc, "cancellable_sse", fake_sse)
    system = "S" * _CACHE_MIN_CHARS
    prov.cache_prompt = True
    r = prov.turn("m", [{"role": "user", "content": "q"}], system, None,
                  lambda _c: None, lambda: False)
    sysmsg = seen["payload"]["messages"][0]
    assert sysmsg["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert r.cached_input_tokens == 800 and r.input_tokens == 900

    prov.cache_prompt = False          # off → plain string on the wire
    prov.turn("m", [{"role": "user", "content": "q"}], system, None,
              lambda _c: None, lambda: False)
    assert seen["payload"]["messages"][0]["content"] == system


def test_cost_report_trims_zeros_with_colours_off(tmp_path, monkeypatch):
    """The total is trimmed BEFORE the colour codes wrap it — rstrip on the
    wrapped string is a no-op with colours on and eats digits with them off
    (NO_COLOR / a pipe), so the two must not disagree."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora import ui
    from aurora.providers import openai_compat as oc
    monkeypatch.setitem(oc.REMOTE_CONTEXT_LIMITS, "p-model",
                        {"model": "p-model", "price_in_per_mtok": 2.0,
                         "price_out_per_mtok": 0.0})
    monkeypatch.setattr(ui, "BOLD", "")
    monkeypatch.setattr(ui, "RESET", "")
    e = _mk_engine(tmp_path, monkeypatch)
    e.session.log("assistant", model="p-model", billed_input=1_000_000,
                  output_tokens=0)
    line = [l for l in ui._cost_report(e, "").splitlines() if "total" in l][0]
    assert line.strip() == "total  $2"
