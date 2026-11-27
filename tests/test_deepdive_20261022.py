"""Regression tests for the 2026-10-22 deep-dive (card 20261022-083449): R319+.
Each was reproduced against the unfixed code first."""

import os

import pytest

from aurora import approve


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    return tmp_path


def _stored_then(first, later, home):
    approve.add_rule("run_command", {"command": first})
    return approve.is_allowed("run_command", {"command": later})


# ── R319: rules never store the harmless half of a payload ─────────────────
@pytest.mark.parametrize("first,later", [
    ("git -C ../lib status", "git -C / clean -fdx"),
    ("git config user.name x", "git config --global alias.st '!rm -rf ~'"),
    ("git config user.name x", "git config core.fsmonitor ./x.sh"),
    ("sqlite3 app.db .tables", "sqlite3 app.db '.shell id'"),
    ("psql -c 'select 1'", "psql -c 'drop table users'"),
    ("mysql -e 'select 1'", "mysql -e 'drop database x'"),
    ("sort -o out.txt in.txt", "sort -o ~/.bashrc in.txt"),
    ("tcsh -c 'echo hi'", "tcsh -c 'rm -rf ~'"),
    ("pwsh -c 1", "pwsh -c 2"),
])
def test_rule_does_not_widen_to_a_payload(home, first, later):
    assert not _stored_then(first, later, home)


def test_the_exact_command_still_matches(home):
    assert _stored_then("git -C ../lib status", "git -C ../lib status", home)


def test_plain_git_subcommands_still_generalize(home):
    assert _stored_then("git status", "git status --short", home)


def test_flag_second_token_is_stored_whole():
    assert approve._rule_for("run_command", {"command": "foo -x bar"}) == "foo -x bar"
    assert approve._rule_for("run_command", {"command": "foo run bar"}) == "foo run"


def test_old_stored_git_rules_become_inert(home):
    approve.save({"run_command": ["git -C", "git config"]})
    assert not approve.is_allowed("run_command", {"command": "git -C / clean -fdx"})
    assert not approve.is_allowed("run_command", {"command": "git config a b"})


# ── R320: web_fetch's approval gate and its fetch agree on "private" ───────
@pytest.mark.parametrize("url", [
    "http://127.1:8765/", "http://2130706433:8765/", "http://0x7f.1/",
    "http://myhost:3000/", "http://localhost/", "http://10.0.0.5/",
])
def test_private_looking_fetch_targets_need_approval(url):
    from aurora import tools
    assert tools.url_needs_approval(url), url


def test_public_fetch_still_needs_no_approval():
    from aurora import tools
    assert not tools.url_needs_approval("https://example.com/page")


def test_fetch_does_not_self_approve_a_dns_private_host(monkeypatch):
    """A public-looking name that RESOLVES private is not 'approved': the
    rebinding guard must still refuse it."""
    from aurora import tools
    from aurora.extensions_bundled import web_extension as w
    monkeypatch.setattr(w, "_peer_is_private", lambda r: True)
    seen = []
    real = tools._private_host
    monkeypatch.setattr(tools, "_private_host",
                        lambda h, resolve=True: seen.append(resolve) or real(h, resolve=False))

    class _R:
        is_redirect = False
        extensions = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(w.httpx, "stream", lambda *a, **k: _R())
    out = w.web_fetch("http://looks-public.example.com/")
    assert out.startswith("[web_fetch blocked")
    assert seen and all(r is False for r in seen)


# ── R321: a read after a write in the same round sees the write ────────────
def test_read_after_edit_in_one_round_sees_the_edit(tmp_path, monkeypatch):
    from aurora import agent
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    f = tmp_path / "a.txt"
    f.write_text("old\n")
    calls = [ToolCall("1", "read_file", {"path": str(tmp_path / "a.txt"), "offset": 1}),
             ToolCall("2", "edit_file", {"path": str(f), "old": "old", "new": "new"}),
             ToolCall("3", "read_file", {"path": str(f)}),
             ToolCall("4", "grep", {"pattern": "new", "path": str(tmp_path)})]
    log = []
    prov = FakeProvider([TurnResult(tool_calls=calls), TurnResult(text="ok")])
    agent.run_turn(prov, "m", [{"role": "user", "content": "go"}], "",
                   _cb(log=log), 5, True)
    results = [o for k, *r in log if k == "result" for o in [r[1]]]
    assert "old" in results[0]
    assert "new" in results[2] and "old" not in results[2]
    assert "a.txt" in results[3]


# ── R322/R323: edits keep the inode; the umask is never toggled ────────────
def test_edit_keeps_inode_and_hard_links(tmp_path):
    import os

    from aurora import paths
    f = tmp_path / "Caddyfile"
    f.write_text("old")
    link = tmp_path / "link"
    os.link(f, link)
    ino = f.stat().st_ino
    paths.write_text_preserving(f, "new content")
    assert f.stat().st_ino == ino
    assert link.read_text() == "new content"
    assert f.read_text() == "new content"


def test_shrinking_edit_leaves_no_tail(tmp_path):
    from aurora import paths
    f = tmp_path / "a"
    f.write_text("a long original line\n")
    paths.write_text_preserving(f, "short")
    assert f.read_text() == "short"


def test_failed_in_place_write_restores_the_original(tmp_path, monkeypatch):
    from aurora import paths
    f = tmp_path / "a"
    f.write_text("original")
    monkeypatch.setattr(paths.os, "fsync",
                        lambda fd: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        paths.write_text_preserving(f, "half")
    assert f.read_text() == "original"


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs /proc (Linux); macOS has no read-only umask accessor")
def test_umask_is_read_without_toggling_it(monkeypatch):
    import os

    from aurora import paths
    monkeypatch.setattr(paths, "_UMASK", None)
    monkeypatch.setattr(os, "umask",
                        lambda m: pytest.fail("umask must not be set"))
    assert paths._umask() == paths._umask() >= 0


# ── R324: the approval preview matches what the tool will do ───────────────
def test_preview_keeps_mixed_line_endings(tmp_path):
    f = tmp_path / "m.txt"
    f.write_bytes(b"a\r\nb\rc\nd\n")
    out = approve.diff_preview("edit_file", {"path": str(f), "old": "d", "new": "D"})
    assert "-d" in out and "+D" in out
    assert "-a" not in out and "-c" not in out       # unchanged lines stay unchanged


def test_preview_says_a_non_unique_edit_will_fail(tmp_path):
    f = tmp_path / "m.txt"
    f.write_text("x\nx\n")
    out = approve.diff_preview("edit_file", {"path": str(f), "old": "x", "new": "y"})
    assert "will fail" in out and "2 times" in out
    out = approve.diff_preview("edit_file", {"path": str(f), "old": "x", "new": "y",
                                             "replace_all": True})
    assert "+y" in out


def test_preview_says_a_missing_old_will_fail(tmp_path):
    f = tmp_path / "m.txt"
    f.write_text("x\n")
    out = approve.diff_preview("edit_file", {"path": str(f), "old": "zzz", "new": "y"})
    assert "will fail" in out and "not found" in out


# ── R325: a keyring that never answers is "no key", once ───────────────────
def test_blocked_keyring_times_out_and_is_not_retried(monkeypatch):
    import sys
    import threading
    import time
    import types

    from aurora import keystore
    monkeypatch.setattr(keystore, "_keyring_stuck", False)
    monkeypatch.setenv("AURORA_KEYRING_TIMEOUT", "0.2")
    calls, gate = [], threading.Event()

    def hang(service, name):
        calls.append(name)
        gate.wait(10)
    monkeypatch.setitem(sys.modules, "keyring",
                        types.SimpleNamespace(get_password=hang))
    t0 = time.monotonic()
    assert keystore._keyring_get("A") is None
    assert time.monotonic() - t0 < 2
    t1 = time.monotonic()
    assert keystore._keyring_get("B") is None        # no second wait
    assert time.monotonic() - t1 < 0.1
    assert calls == ["A"]
    gate.set()


def test_keyring_answer_passes_through(monkeypatch):
    import sys
    import types

    from aurora import keystore
    monkeypatch.setattr(keystore, "_keyring_stuck", False)
    monkeypatch.setitem(sys.modules, "keyring",
                        types.SimpleNamespace(get_password=lambda s, n: "v-" + n))
    assert keystore._keyring_get("K") == "v-K"


# ── R326: the /props health probe uses the provider's pooled client ────────
def test_props_probe_uses_the_pooled_client(tmp_path, monkeypatch):
    import httpx

    from tests.test_core import _mk_engine
    e = _mk_engine(tmp_path, monkeypatch)
    e.current = {"model": "local", "provider": "local"}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("bare httpx.get"))
    used = []

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"default_generation_settings": {"n_ctx": 8},
                                "model_path": "/m/x.gguf"}
    prov = e._provider_for(e.current)
    monkeypatch.setattr(prov, "pick_endpoint", lambda cache_ok=True: prov.base_url)
    monkeypatch.setattr(prov, "_client_for", lambda url: type(
        "C", (), {"get": lambda self, *a, **k: used.append(a) or _R()})())
    assert e._provider_health_uncached()["ok"] is True
    assert used


# ── R327: read_file pages without decoding the lines it skips; tail= ───────
@pytest.fixture
def biglog(tmp_path):
    f = tmp_path / "big.log"
    f.write_text("".join(f"line {i}\n" for i in range(1, 200_001)))
    return f


def test_paging_deep_into_a_file_is_correct(biglog):
    from aurora import tools
    out = tools.read_file(str(biglog), offset=150_000, limit=2)
    assert out.splitlines()[1:] == ["line 150000", "line 150001"]
    assert out.startswith("[lines 150000-150001")


def test_paging_past_the_end_says_so(biglog):
    from aurora import tools
    assert "fewer than" in tools.read_file(str(biglog), offset=300_000, limit=5)


def test_paging_skips_multibyte_and_crlf_lines_correctly(tmp_path):
    from aurora import tools
    f = tmp_path / "m.txt"
    f.write_bytes("é1\r\né2\r\né3\r\n".encode())
    assert tools.read_file(str(f), offset=2, limit=1).splitlines()[1] == "é2"


def test_tail_returns_the_last_lines(biglog):
    from aurora import tools
    out = tools.read_file(str(biglog), tail=3)
    assert out.splitlines()[1:] == ["line 199998", "line 199999", "line 200000"]


def test_tail_of_a_short_file_reports_the_total(tmp_path):
    from aurora import tools
    f = tmp_path / "s.txt"
    f.write_text("a\nb\n")
    out = tools.read_file(str(f), tail=10)
    assert out.splitlines() == ["[last 2 lines of 2]", "a", "b"]


# ── R328/R329: fewer false secret challenges; "keep" is remembered ─────────
from aurora import secrets as secretscan  # noqa: E402


@pytest.mark.parametrize("text", [
    "see double-Esc-within-2s here",
    '            password=Condition(lambda: x)',
    '                draft_tokens = ""',
    '                draft_tokens = f"up{n}"',
    "api_key = None",
])
def test_code_and_identifiers_are_not_secrets(text):
    assert secretscan.scan(text) == []


@pytest.mark.parametrize("text", [
    "API_KEY=abcdef123456",
    "export SECRET_TOKEN='s3cr3tvalue99'",
    "tok aB3xK9mQ2pL7vN4zR8tY1wC5",
    "see aB3-xK9mQ2-pL7vN4zR8tY1wC5 here",
])
def test_real_looking_secrets_still_match(text):
    assert secretscan.scan(text)


def test_keep_on_tool_output_is_remembered_for_the_session(tmp_path, monkeypatch):
    from aurora import agent
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb
    f = tmp_path / ".env"
    f.write_text("API_KEY=abcdef123456\n")
    asked = []
    keep = set()
    cb = _cb()
    cb.secret_challenge = lambda ctx, m, **k: (asked.append(ctx), keep.update(
        secretscan.hash_value(x.text) for x in m), "keep")[2]
    cb.secret_session_keep = keep
    for _ in range(2):
        calls = [ToolCall("1", "read_file", {"path": str(f)})]
        prov = FakeProvider([TurnResult(tool_calls=calls), TurnResult(text="ok")])
        agent.run_turn(prov, "m", [{"role": "user", "content": "go"}], "", cb, 5, True)
    assert asked == ["tool:read_file"]            # second read: not asked again


# ── R330/R331: a dead model is flagged; narrow terminals get ellipses ──────
def test_ellipsize_middle_keeps_head_and_tail():
    from aurora.tui import _ellipsize_middle
    p = "/tmp/session-scratch/deep/ux/work"
    out = _ellipsize_middle(p, 24)
    assert len(out) == 24 and "…" in out
    assert out.endswith("ux/work") and out.startswith("/tmp")
    assert _ellipsize_middle("/short", 24) == "/short"


def test_clip_frags_cuts_each_row_with_an_ellipsis_and_keeps_handlers():
    from aurora.tui import _clip_frags
    h = object()
    frags = [("class:status", " a-really-long-model-name-here"), ("", "\n\n"),
             ("class:status.hint", " / commands", h),
             ("class:status.hint", " · "),
             ("class:status.hint", "Ctrl+J newline"),
             ("class:status.hint", " · "),
             ("class:status.hint", "? Help")]
    out = _clip_frags(frags, 20)
    rows = "".join(f[1] for f in out).split("\n")
    assert all(len(r) <= 20 for r in rows)
    assert rows[0].endswith("…")                       # plain row: cut + …
    assert rows[2] == " / commands …"                  # hint row: whole hints
    assert [f for f in out if f[1] == " / commands"][0][2] is h


def test_clip_frags_leaves_a_fitting_bar_alone():
    from aurora.tui import _clip_frags
    frags = [("a", " ok"), ("", "\n\n"), ("b", "fine")]
    assert [f[:2] for f in _clip_frags(frags, 80)] == \
        [("a", " ok"), ("", "\n"), ("", "\n"), ("b", "fine")]


def test_provider_health_is_remembered_for_the_status_bar(tmp_path, monkeypatch):
    from tests.test_core import _mk_engine
    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(e, "_provider_health_uncached",
                        lambda: {"ok": False, "detail": "unreachable: refused"})
    e.provider_health()
    assert e.last_health["ok"] is False
    assert e.last_health["model"] == e.current.get("model")


def test_completion_menu_fills_its_row():
    from aurora import tui
    menu = tui._completions_menu()
    assert not menu.content.dont_extend_width()
    ctrl = menu.content.content
    assert isinstance(ctrl, tui._SafeCompletionsMenuControl)


def test_help_columns_line_up_and_copy_last_wording_agrees():
    import re

    from aurora import ui
    for line in ui.HELP.splitlines():
        plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
        if plain.startswith(" ") or len(plain) < 23:
            continue
        assert plain[21] == " " and plain[22] != " ", plain
    assert "turn · full · all · bash · last" in re.sub(r"\x1b\[[0-9;]*m", "", ui.HELP)   # R453


# ── R334/R335/R336: session allow, scope preview, allowlist audit ──────────
@pytest.fixture
def clean_session():
    approve.clear_session_rules()
    yield
    approve.clear_session_rules()


def test_session_rule_allows_without_saving(home, clean_session):
    approve.add_session_rule("run_command", {"command": "make test"})
    # `make` is in DANGEROUS_COMMANDS, so it is exact-only even for a session
    assert approve.is_allowed("run_command", {"command": "make test"})
    assert not approve.is_allowed("run_command", {"command": "make install"})
    assert approve.load().get("run_command", []) == []
    assert approve.session_rules() == {"run_command": ["make test"]}
    approve.clear_session_rules()
    assert not approve.is_allowed("run_command", {"command": "make test"})


def test_session_rule_uses_the_same_safety_scoping(home, clean_session):
    approve.add_session_rule("run_command", {"command": "git -C ../lib status"})
    assert not approve.is_allowed("run_command", {"command": "git -C / clean -fdx"})


def test_choosing_t_at_the_prompt_runs_and_remembers(tmp_path, monkeypatch, clean_session):
    from aurora import agent
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    asked = []
    cb = _cb()
    cb.approve = lambda t, a, d: asked.append(a["command"]) or "t"
    for _ in range(2):
        calls = [ToolCall("1", "run_command", {"command": "echo hi there"})]
        prov = FakeProvider([TurnResult(tool_calls=calls), TurnResult(text="ok")])
        agent.run_turn(prov, "m", [{"role": "user", "content": "go"}], "", cb, 5, True)
    assert asked == ["echo hi there"]                 # second run: not asked
    assert approve.load().get("run_command", []) == []


@pytest.mark.parametrize("cmd,desc", [
    ("ls -la", "any arguments"),
    ("git status --short", "any command starting `git status`"),
    ("rm -rf ./build", "exactly this command"),
    ("sort -o out in", "exactly this command"),
])
def test_rule_scope_describes_the_reach(cmd, desc):
    rule, text = approve.rule_scope("run_command", {"command": cmd})
    assert rule and desc in text


def test_audit_and_prune_find_stale_rules(home):
    approve.save({"run_command": ["git status", "git status", "rm", "ls",
                                  "mv x", "sort -o", "pytest -q"]})
    found = dict(approve.audit()["run_command"])
    assert found == {"git status": "duplicate", "rm": "legacy",
                     "mv x": "exact-only", "sort -o": "exact-only",
                     "pytest -q": "exact-only"}
    assert approve.prune() == 5
    assert approve.load()["run_command"] == ["git status", "ls"]
    assert approve.audit() == {}


# ── R339: plan mode ────────────────────────────────────────────────────────
def test_plan_mode_refuses_gated_calls_but_allows_reads(tmp_path, monkeypatch):
    from aurora import agent
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    r = tmp_path / "r.txt"
    r.write_text("readable")
    w = tmp_path / "w.txt"
    asked, log = [], []
    cb = _cb(log=log)
    cb.approve = lambda t, a, d: asked.append(t) or "y"
    cb.plan_mode = lambda: True
    calls = [ToolCall("1", "read_file", {"path": str(r)}),
             ToolCall("2", "write_file", {"path": str(w), "content": "x"}),
             ToolCall("3", "run_command", {"command": "echo hi"})]
    prov = FakeProvider([TurnResult(tool_calls=calls), TurnResult(text="plan")])
    agent.run_turn(prov, "m", [{"role": "user", "content": "go"}], "", cb, 5, True)
    results = [x[2] for x in log if x[0] == "result"]
    assert "readable" in results[0]
    assert results[1].startswith("[plan mode") and results[2].startswith("[plan mode")
    assert not w.exists() and asked == []


def test_plan_mode_beats_an_allowlist_rule(tmp_path, monkeypatch):
    from aurora import agent
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    approve.save({"run_command": ["echo hi"]})
    log = []
    cb = _cb(log=log)
    cb.plan_mode = lambda: True
    prov = FakeProvider([TurnResult(tool_calls=[ToolCall("1", "run_command", {"command": "echo hi"})]),
                         TurnResult(text="ok")])
    agent.run_turn(prov, "m", [{"role": "user", "content": "go"}], "", cb, 5, True)
    assert [x[2] for x in log if x[0] == "result"][0].startswith("[plan mode")


def test_plan_mode_is_session_only_and_off_by_default(tmp_path, monkeypatch):
    from tests.test_core import _mk_engine
    assert _mk_engine(tmp_path, monkeypatch).plan_mode is False


# ── R341: the model picker can add a model ─────────────────────────────────
def test_model_picker_has_an_add_row_that_runs_model_add(tmp_path, monkeypatch, capsys):
    from aurora import ui
    from tests.test_core import _mk_engine
    e = _mk_engine(tmp_path, monkeypatch)
    added = []
    monkeypatch.setattr(ui, "_add_model_cmd", lambda eng, arg: added.append(arg))
    answers = iter(["add", "acme/new-model"])
    monkeypatch.setattr(ui, "select", lambda prompt, options, **k: (
        capsys.readouterr() and any(o[0] == "add" for o in options) and next(answers)))
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    ui._pick_model(e, ui.TerminalFrontend())
    assert added == ["acme/new-model"]


def test_deferred_pick_does_not_offer_add(tmp_path, monkeypatch):
    from aurora import ui
    from tests.test_core import _mk_engine
    e = _mk_engine(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(ui, "select", lambda prompt, options, **k: (
        seen.append([o[0] for o in options]), None)[1])
    ui._pick_model(e, ui.TerminalFrontend(), on_pick=lambda p: None)
    assert seen and "add" not in seen[0]


# ── R340: spend caps ───────────────────────────────────────────────────────
@pytest.fixture
def eng(tmp_path, monkeypatch):
    from tests.test_core import _mk_engine
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "ah"))
    return _mk_engine(tmp_path, monkeypatch)


def test_no_cap_means_ok(eng):
    eng._cost = 999.0
    assert eng.budget_check() == ("ok", "")


def test_session_cap_warns_once_at_80_percent_then_stops(eng):
    eng.set_session_budget(1.0)
    eng._cost = 0.5
    assert eng.budget_check()[0] == "ok"
    eng._cost = 0.85
    st, msg = eng.budget_check()
    assert st == "warn" and "85%" in msg
    assert eng.budget_check()[0] == "ok"            # warned once
    eng._cost = 1.0
    st, msg = eng.budget_check()
    assert st == "stop" and "session spend limit" in msg


def test_daily_cap_uses_the_ledger_across_sessions(eng):
    eng.set_daily_budget(2.0)
    eng._ledger_add(1.5)
    eng._ledger_add(0.6)
    assert eng.spent_today() == pytest.approx(2.1)
    assert eng.budget_check()[0] == "stop"
    eng.set_daily_budget(0)
    assert eng.budget_check()[0] == "ok"


def test_daily_cap_is_persisted(eng, tmp_path, monkeypatch):
    from aurora.engine import Engine
    eng.set_daily_budget(3.5)
    assert Engine(str(tmp_path / "config.yaml")).daily_budget == 3.5


def test_send_is_refused_over_the_cap(eng):
    notes = []
    fe = type("F", (), {"notify": staticmethod(notes.append)})()
    eng.set_session_budget(0.01)
    eng._cost = 0.5
    eng.send("hello", fe)
    assert notes and "spend limit" in notes[0]
    assert eng.messages == []


def test_a_round_over_the_cap_skips_its_tool_calls(tmp_path, monkeypatch):
    from aurora import agent
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb
    w = tmp_path / "w.txt"
    log = []
    cb = _cb(log=log)
    cb.budget = lambda: ("stop", "stopped: daily spend limit $1.00 reached")
    prov = FakeProvider([TurnResult(tool_calls=[ToolCall("1", "write_file",
                         {"path": str(w), "content": "x"})]), TurnResult(text="no")])
    msgs = [{"role": "user", "content": "go"}]
    agent.run_turn(prov, "m", msgs, "", cb, 5, True)
    assert not w.exists()
    assert ("notify", "stopped: daily spend limit $1.00 reached") in log
    assert "spend limit reached" in str(msgs)


# ── R342: /try sandbox ─────────────────────────────────────────────────────
@pytest.fixture
def repo(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "ah"))
    r = tmp_path / "repo"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), "-c", "user.name=t",
                                   "-c", "user.email=t@t", *a], check=True,
                                  capture_output=True)
    g("init", "-q")
    (r / "a.txt").write_text("one\n")
    g("add", "a.txt")
    g("commit", "-qm", "init")
    return r


def test_sandbox_carries_dirty_state_and_diffs_only_the_trial(repo):
    from aurora import trybox
    (repo / "a.txt").write_text("one\nmine\n")        # my uncommitted edit
    (repo / "new.txt").write_text("untracked\n")
    sb = trybox.create(repo)
    assert (sb.path / "a.txt").read_text() == "one\nmine\n"
    assert (sb.path / "new.txt").read_text() == "untracked\n"
    assert trybox.diff(sb) == ""                       # baseline == my state
    (sb.path / "a.txt").write_text("one\nmine\ntrial\n")
    (sb.path / "b.txt").write_text("brand new\n")
    d = trybox.diff(sb)
    assert "+trial" in d and "+brand new" in d and "+mine" not in d
    trybox.discard(sb)
    assert not sb.path.exists()
    assert (repo / "a.txt").read_text() == "one\nmine\n"   # untouched


def test_apply_copies_only_the_trial_changes_back(repo):
    from aurora import trybox
    (repo / "a.txt").write_text("one\nmine\n")
    sb = trybox.create(repo)
    (sb.path / "a.txt").write_text("one\nmine\ntrial\n")
    (sb.path / "b.txt").write_text("brand new\n")
    assert trybox.apply(sb) == "applied"
    assert (repo / "a.txt").read_text() == "one\nmine\ntrial\n"
    assert (repo / "b.txt").read_text() == "brand new\n"
    assert not sb.path.exists()


def test_run_in_changes_cwd_and_restores_it(repo):
    import os

    from aurora import trybox
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "m.py").write_text("x=1\n")
    sb = trybox.create(sub)
    before = os.getcwd()
    seen = trybox.run_in(sb, os.getcwd)
    assert seen == str(sb.path / "pkg") or os.path.realpath(seen) == os.path.realpath(sb.path / "pkg")
    assert os.getcwd() == before
    trybox.discard(sb)


def test_try_outside_a_repo_is_a_clear_error(tmp_path, monkeypatch):
    from aurora import trybox
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "ah"))
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(trybox.TryError, match="git repository"):
        trybox.create(d)


# ── R343: say so when the local model has to wake up ───────────────────────
def test_send_warns_when_the_local_server_is_asleep(eng):
    notes = []
    fe = type("F", (), {"notify": staticmethod(notes.append)})()
    eng.current = {"model": "local", "provider": "local"}
    eng.provider_health = lambda timeout=4.0: {"ok": True, "detail": "asleep — the model loads"}
    eng._warn_if_asleep(fe)
    assert notes and "asleep" in notes[0] and "waking" in notes[0]


def test_awake_server_is_checked_at_most_every_30s(eng):
    calls = []
    fe = type("F", (), {"notify": staticmethod(lambda m: None)})()
    eng.current = {"model": "local", "provider": "local"}
    eng.provider_health = lambda timeout=4.0: calls.append(1) or {"ok": True, "detail": "x ready"}
    eng._warn_if_asleep(fe)
    eng._warn_if_asleep(fe)
    assert len(calls) == 1


def test_remote_models_are_never_probed_for_sleep(eng):
    fe = type("F", (), {"notify": staticmethod(lambda m: None)})()
    eng.current = {"model": "some/remote", "provider": "openrouter"}
    eng.provider_health = lambda timeout=4.0: pytest.fail("probed")
    eng._warn_if_asleep(fe)


# ── R344: connection errors read as sentences ──────────────────────────────
@pytest.mark.parametrize("err,expect", [
    (OSError("[Errno 111] Connection refused"), "connection refused — is the server running?"),
    (OSError("timed out"), "no answer in time"),
    (OSError("[Errno -2] Name or service not known"), "does not resolve"),
    (OSError("weird failure"), "weird failure"),
])
def test_unreachable_message_is_a_sentence(err, expect):
    from aurora.engine import _unreachable
    out = _unreachable(err, "http://127.0.0.1:8080/v1")
    assert out.startswith("unreachable: ") and expect in out
    assert "Errno" not in out


# ── R348: type while the model is working ─────────────────────────────────
@pytest.mark.parametrize("line", [
    "/auto-approve on", "/auto-approve off", "/thinking", "/markdown",
    "/fallback on", "/cache off", "/redact", "/autocompact on", "/multiline off",
])
def test_r346_toggles_run_now_while_busy(line):
    from aurora import ui
    assert ui.side_safe_command(line)


@pytest.mark.parametrize("line", ["/clear", "/compact", "/rewind", "/undo",
                                  "/redact allowlist clear", "/fallback maybe"])
def test_r346_history_commands_still_wait(line):
    from aurora import ui
    assert not ui.side_safe_command(line)


def test_r346_steer_notes_reach_the_models_next_result():
    from aurora import engine
    e = engine.Engine.__new__(engine.Engine)
    e._steer_notes, e._steer_lock = [], engine._Lock()
    e.add_steer("use rg, not grep")
    e.add_steer("and skip tests/")
    assert e.take_steer() == ["use rg, not grep", "and skip tests/"]
    assert e.take_steer() == []


@pytest.mark.python_loop  # steering is not in aurora-core/1 (the adapter says so)
def test_r346_steer_rides_the_rounds_last_tool_result(tmp_path, monkeypatch):
    from aurora import agent
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    f = tmp_path / "a.txt"
    f.write_text("x\n")
    notes = [["prefer rg"], []]
    cb = _cb(log=[])
    cb.steer = lambda: notes.pop(0) if notes else []
    prov = FakeProvider([TurnResult(tool_calls=[ToolCall("1", "read_file", {"path": str(f)})]),
                         TurnResult(text="ok")])
    msgs = [{"role": "user", "content": "go"}]
    agent.run_turn(prov, "m", msgs, "", cb, 5, True)
    assert "[user guidance: prefer rg]" in str(msgs)


def test_missing_config_message_names_the_fix(monkeypatch):
    from pathlib import Path

    from aurora import __main__ as m
    monkeypatch.delenv("AURORA_CONFIG", raising=False)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    with pytest.raises(SystemExit) as e:
        m._default_config()
    assert "config.yaml.example" in str(e.value)
