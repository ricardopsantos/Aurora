"""R536-R555: 20 more bugs, plus tests for modules that had few (htmltext, diagnose, tokens, mdrender, modelcfg,
trybox, gitcommit, headless, bashmode, exthost, keystore, ctxtree, web shares)."""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from aurora import bashmode, colors, diagnose, gitcommit, headless, htmltext, mdrender, modelcfg, tokens

HAS_GIT = shutil.which("git") is not None


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t.invalid"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=r, check=True)
    (r / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
    return r


# ── R536 / R537: /try sandboxes ──
@pytest.mark.skipif(not HAS_GIT, reason="git")
def test_r536_two_tries_in_one_second(tmp_path, monkeypatch):
    from aurora import trybox
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(trybox, "aurora_home", lambda: tmp_path / "home")
    monkeypatch.setattr(trybox.time, "strftime", lambda f: "20260101-000000")
    r = _repo(tmp_path)
    a, b = trybox.create(r), trybox.create(r)
    try:
        assert a.path != b.path and a.path.is_dir() and b.path.is_dir()
    finally:
        trybox.discard(a)
        trybox.discard(b)


@pytest.mark.skipif(not HAS_GIT, reason="git")
def test_r537_untracked_symlink_kept_and_apply_roundtrip(tmp_path, monkeypatch):
    from aurora import trybox
    monkeypatch.setattr(trybox, "aurora_home", lambda: tmp_path / "home")
    r = _repo(tmp_path)
    (r / "sub").mkdir()
    (r / "sub" / "f").write_text("x")
    os.symlink("sub", r / "link")
    (r / "a.txt").write_text("dirty\n")                  # uncommitted edit is in the baseline
    sb = trybox.create(r)
    assert (sb.path / "link").is_symlink() and os.readlink(sb.path / "link") == "sub"
    assert (sb.path / "a.txt").read_text() == "dirty\n"
    assert trybox.diff(sb) == ""                         # the user's own edits are not "the turn's" changes
    (sb.path / "new.txt").write_text("n\n")
    assert "new.txt" in trybox.diff(sb, stat_only=True)
    assert trybox.apply(sb) == "applied" and (r / "new.txt").read_text() == "n\n"
    assert not sb.path.exists()


def test_trybox_needs_a_repo(tmp_path):
    from aurora import trybox
    if not HAS_GIT:
        pytest.skip("git")
    with pytest.raises(trybox.TryError, match="git repository"):
        trybox.toplevel(tmp_path)


# ── R538 / R539: HTML to text ──
def test_r538_lone_less_than_is_text():
    assert htmltext._flatten("a < b and c > d") == "a < b and c > d"
    assert htmltext._flatten("1 <2 x") == "1 <2 x"
    assert htmltext._flatten("<b>x</b><p>y</p>") == "x\ny\n"


def test_r539_tag_name_must_end():
    s = htmltext._strip_blocks("<scripts>keep</scripts><script src=x>drop</script><SCRIPT>drop</SCRIPT>tail",
                               ("script",))
    assert s == "<scripts>keep</scripts>tail"
    assert htmltext._strip_blocks("<stylesheet>k</stylesheet><style>.a{}</style>", ("style",)) == "<stylesheet>k</stylesheet>"


def test_htmltext_unclosed_is_linear():
    t = time.perf_counter()
    htmltext._strip_blocks("<script " * 50_000, ("script", "style"))
    htmltext._flatten("<!-- " * 50_000)
    htmltext._strip_comments("<!--" * 50_000)
    assert time.perf_counter() - t < 2
    assert htmltext._flatten("<ul><li>a<li>b</ul>") == "\n\n- a\n- b\n"
    assert htmltext._strip_comments("a<!-- x -->b<!-- open") == "ab"


# ── R540: failure diagnosis ──
@pytest.mark.parametrize("msg,kind", [
    ("HTTP 401 Unauthorized", "auth"), ("status 404", "notfound"), ("429 Too Many Requests", "ratelimit"),
    ("connect to host:4013 failed oddly", "other"), ("used 4404 tokens", "other"), ("v1.401 build", "other"),
    ("Connection refused", "refused"), ("read timed out", "timeout"),
    ("[Errno -2] Name or service not known", "dns"), ("SSL: CERTIFICATE_VERIFY_FAILED", "tls"),
])
def test_r540_classify(msg, kind):
    assert diagnose.classify(msg) == kind


def test_diagnose_every_kind_has_fixes():
    for k in diagnose.KINDS:
        d = diagnose.diagnose(k, provider="p", url="http://h", model="m", key_env="K", is_ollama=(k == "refused"))
        assert d.kind == k and d.cause and d.fixes
        lines = diagnose.render(d)
        assert lines[0].strip().startswith("cause:") and "fix:" in lines[1]
    assert "ollama serve" in diagnose.diagnose("refused", is_ollama=True).fixes[0]
    assert "ollama pull m" in diagnose.diagnose("notfound", model="m", is_ollama=True).fixes[0]


# ── R541: /commit helpers ──
def test_r541_git_timeout_never_raises(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("git", 30)
    monkeypatch.setattr(gitcommit.subprocess, "run", slow)
    assert gitcommit.staged_diff(".") == "" and gitcommit.unstaged_summary(".") == "" and gitcommit.recent_log(".") == ""
    assert gitcommit.commit("m").startswith("commit failed")
    with pytest.raises(gitcommit.GitError):
        gitcommit.stage_all(".")


@pytest.mark.skipif(not HAS_GIT, reason="git")
def test_gitcommit_flow(tmp_path):
    r = str(_repo(tmp_path))
    assert gitcommit.is_repo(r) and not gitcommit.is_repo(str(tmp_path))
    (tmp_path / "repo" / "b.txt").write_text("é\n".encode("latin-1").decode("latin-1"))
    assert "b.txt" in gitcommit.unstaged_summary(r)
    gitcommit.stage_all(r)
    assert "b.txt" in gitcommit.staged_diff(r)
    assert gitcommit.commit("add b", r).startswith("committed ")
    assert gitcommit.recent_log(r).splitlines()[0] == "add b"


def test_gitcommit_draft_is_capped(monkeypatch):
    seen = {}
    monkeypatch.setattr(gitcommit, "side_completion", lambda p, m, ask: seen.setdefault("ask", ask) and "msg")

    class E:
        current = {"model": "m"}

        def _provider_for(self, cur, interactive=True):
            return None
    assert gitcommit.draft_message(E(), "x" * 50_000, "") == "msg"
    assert len(seen["ask"]) < gitcommit._DRAFT_DIFF_CAP + 2000 and "(truncated)" in seen["ask"]
    assert "(no history yet)" in seen["ask"]


# ── R542: terminal markdown ──
def test_r542_code_span_not_bolded(monkeypatch):
    monkeypatch.setattr(mdrender, "RESET", "</>")
    monkeypatch.setattr(mdrender, "BOLD", "<b>")
    monkeypatch.setattr(mdrender, "CYAN", "<c>")
    monkeypatch.setattr(mdrender, "linkify", lambda s: s)
    r = mdrender.LineRenderer()
    assert r.render("use `a**b**c` and **bold**") == "use <c>a**b**c</> and <b>bold</>"
    assert r.render("- item") == "• item"
    assert r.render("## Head\n").endswith("Head</>\n")


def test_mdrender_fences_and_plain(monkeypatch):
    r = mdrender.LineRenderer()
    monkeypatch.setattr(mdrender, "RESET", "")
    assert r.render("**x**") == "**x**"                  # colours off: byte-faithful
    monkeypatch.setattr(mdrender, "RESET", "\x1b[0m")
    r.render("```py")
    assert r.in_fence and r.fence_lang == "python"
    out = r.render("def f(): return 1  # c")
    assert mdrender.MAGENTA in out and "# c" in out
    r.render("```")
    assert not r.in_fence and r.fence_lang is None
    assert mdrender._resolve_lang("cobol") is None and mdrender._resolve_lang("TSX") == "javascript"


# ── R543: escape stripping ──
def test_r543_c1_introducers_stripped():
    assert colors.strip_dangerous_escapes("a\x9d52;c;QQ==\x07b") == "ab"
    assert colors.strip_dangerous_escapes("a\x90qdata\x9cb") == "ab"
    assert colors.strip_dangerous_escapes("a\x9d0;never ends") == "a"
    assert colors.strip_dangerous_escapes("\x1b[31mred\x1b[0m") == "\x1b[31mred\x1b[0m"
    assert colors.visible("a\x1b[8mb‮") == "a\\x1b[8mb\\u202e"


# ── R544: token counts ──
@pytest.mark.parametrize("n,s", [(0, "0"), (950, "950"), (1000, "1k"), (1500, "1.5k"), (999_949, "999.9k"),
                                 (999_950, "1M"), (1_234_567, "1.2M"), (12_000_000, "12M")])
def test_r544_fmt_token_count(n, s):
    assert tokens.fmt_token_count(n) == s


def test_tokens_estimates():
    assert tokens.estimate_tokens("abcd" * 10) == 10 == tokens.estimate_tokens_from_chars(40)


# ── R545 / R546: headless runs ──
class _Eng:
    def __init__(self, act):
        self.act, self.current, self.session = act, {"model": "m"}, None
        self.max_iterations = self.auto_approve = self.headless = None

    def send(self, task, fe):
        self.act(fe)

    def context_stats(self):
        raise RuntimeError


def test_r545_headless_from_a_thread(monkeypatch, capsys):
    out = {}
    eng = _Eng(lambda fe: fe.on_text("hello"))
    t = threading.Thread(target=lambda: out.setdefault("code", headless.run(eng, "x")))
    t.start()
    t.join()
    assert out["code"] == 0 and "hello" in capsys.readouterr().out


def test_r546_headless_progress_is_escaped():
    err = io.StringIO()
    fe = headless.HeadlessFrontend(out=io.StringIO(), err=err)
    fe.on_tool_start("run_command", {"command": "ls\x1b]52;c;QQ==\x07"})
    fe.on_tool_result("run_command", "\x1b[2Jout")
    assert "\x1b" not in err.getvalue() and "\\x1b" in err.getvalue()


def test_headless_outcomes():
    fe = headless.HeadlessFrontend(out=io.StringIO(), err=io.StringIO())
    assert fe.approve("run_command", {}, "")[0] == "n" and fe.failed
    assert headless.HeadlessFrontend(out=io.StringIO(), err=io.StringIO(), yes=True).approve("t", {}, "") == "y"
    fe = headless.HeadlessFrontend(out=io.StringIO(), err=io.StringIO())
    fe.on_tool_result("w", "[denied by policy: x]")
    assert fe.reasons == ["w denied by policy"]
    o = io.StringIO()
    eng = _Eng(lambda fe: None)
    fe2 = headless.HeadlessFrontend(out=o, err=io.StringIO(), as_json=True)
    fe2.on_text("a")
    rep = headless._report(eng, fe2, 0)
    assert rep["reply"] == "a" and rep["cost_usd"] is None and rep["ok"]


def test_headless_json_no_response(monkeypatch, capsys):
    code = headless.run(_Eng(lambda fe: None), "x", as_json=True)
    rep = json.loads(capsys.readouterr().out)
    assert code == 1 and rep["reasons"] == ["the model produced no response"]


# ── R547: extension host ──
def test_r547_exthost_stderr_not_a_pipe(tmp_path):
    from aurora import exthost
    prog = tmp_path / "noisy.py"
    prog.write_text("import sys\nsys.stderr.write('x' * 300000)\nsys.stderr.flush()\n"
                    "line = sys.stdin.readline()\nprint('{\"jsonrpc\": \"2.0\", \"id\": 1, \"result\": 1}', flush=True)\n")
    c = exthost.Client([sys.executable, str(prog)], timeout=10)
    try:
        assert c.request("x")["result"] == 1
    finally:
        c.close()


def test_exthost_reports_exit(tmp_path):
    from aurora import exthost
    prog = tmp_path / "die.py"
    prog.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
    c = exthost.Client([sys.executable, str(prog)], timeout=10)
    try:
        with pytest.raises(ConnectionError, match="boom"):
            c.request("x")
    finally:
        c.close()


# ── R548-R550: bash mode ──
def test_r548_copy_drops_hyperlinks():
    s = "\x1b]8;;file:///x\x1b\\name\x1b]8;;\x1b\\ \x1b[1mb\x1b[0m"
    assert bashmode.plain_text(s) == "name b"


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters any folder")
def test_r549_cd_without_permission(tmp_path):
    d = tmp_path / "locked"
    d.mkdir()
    d.chmod(0o600)
    try:
        st = bashmode.initial_state(str(tmp_path))
        new, err = bashmode.cd_into(st, "locked")
        assert err and "permission denied" in err and new is st
    finally:
        d.chmod(0o700)


def test_r550_quoted_cd_goes_to_bash(tmp_path):
    for t in ('"my dir"', "'my dir'", "my\\ dir", "$HOME", "-", "~other", "*"):
        assert bashmode.plain_cd_target(t) is None, t
    for t in ("my dir", "~", "~/x", "../a"):
        assert bashmode.plain_cd_target(t) == t
    (tmp_path / "my dir").mkdir()
    st = bashmode.initial_state(str(tmp_path))
    new, err = bashmode.cd_into(st, "my dir")
    assert err is None and new.cwd == str(tmp_path / "my dir") and new.env["OLDPWD"] == str(tmp_path)
    assert bashmode.cd_into(st, "nope")[1].startswith("cd: no such")


def test_bashmode_format_result():
    R = bashmode.Result
    assert bashmode.format_result(R("ok\n", 0)) == "ok"
    assert bashmode.format_result(R("x", 2)) == "x\n[exit 2]"
    assert bashmode.format_result(R("", None), timeout=5) == "[timeout after 5s]"
    assert bashmode.format_result(R("p", None, cancelled=True)) == "p\n[cancelled]"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash")
def test_bashmode_state_carries(tmp_path):
    (tmp_path / "d").mkdir()
    st = bashmode.initial_state(str(tmp_path))
    res, st2 = bashmode.run("cd d && export AURORA_T=1 && echo hi", st, timeout=20)
    assert res.code == 0 and "hi" in res.out and st2.cwd == str(tmp_path / "d") and st2.env.get("AURORA_T") == "1"
    res, st3 = bashmode.run("cd /", st2, timeout=20, carry=False)
    assert st3 is st2


# ── R551 / R552: key store ──
def test_r551_r552_salt_and_derivation(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from aurora import keystore
    monkeypatch.setattr(keystore, "aurora_home", lambda: tmp_path)
    keystore._fernet_cache.clear()
    keystore._encfile_save("pw", {"K": "v"})
    import cryptography.hazmat.primitives.kdf.pbkdf2 as kdf
    calls = []
    real = kdf.PBKDF2HMAC.derive
    monkeypatch.setattr(kdf.PBKDF2HMAC, "derive", lambda self, b: calls.append(1) or real(self, b))
    assert keystore._encfile_load("pw") == {"K": "v"}
    keystore._encfile_save("pw", {"K": "w"})
    assert calls == []                                    # derived once, at the first save
    (tmp_path / "keys.salt").unlink()
    keystore._fernet_cache.clear()
    with pytest.raises(ValueError, match="keys.salt is missing"):
        keystore._encfile_load("pw")
    assert not (tmp_path / "keys.salt").exists()


# ── R553: /context labels ──
def test_r553_context_label_escaped(tmp_path, monkeypatch):
    from aurora import ctxtree, session
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    s = session.Session()
    s.log("user", text="hi \x1b[8mhidden", model="m")
    s.log("assistant", text="ok", model="m")
    out = ctxtree.render(s.id) if hasattr(ctxtree, "render") else ctxtree.report(s.id)
    text = "\n".join(out) if isinstance(out, list) else str(out)
    assert "\\x1b[8m" in text and "\x1b[8mhidden" not in text


# ── R554 / R555: share links ──
def test_r554_r555_share_expiry(tmp_path, monkeypatch):
    from aurora.web import shares
    monkeypatch.setattr(shares, "aurora_home", lambda: tmp_path)
    assert shares._expiry(float("inf")) <= time.time() + 3651 * 86400
    assert shares._expiry(1e300) > 0 and shares._expiry(float("nan")) == 0 and shares._expiry(0) == 0
    tok = "A" * 22
    (tmp_path / "web-shares.json").write_text(json.dumps({tok: {"session": "s", "upto": "x", "expires": 0}}))
    assert shares.info(tok) is None and shares.lookup(tok) is None
    assert shares.info("../etc") is None


# ── modelcfg: comment-preserving edits ──
CFG = """# top comment
runtime:
  timeout: 30
models:
  # the local one
  - model: local   # keep me
    provider: llama
  - model: big
    provider: openrouter
other: 1
"""


def test_modelcfg_add_update_delete_keep_comments():
    t = modelcfg.add(CFG, {"model": "new", "provider": "x"})
    assert "# the local one" in t and "# keep me" in t and modelcfg.entries(t)[-1] == {"model": "new", "provider": "x"}
    assert t.index("model: new") < t.index("other: 1")
    u = modelcfg.update(t, 0, {"model": "local2", "provider": "llama"})
    assert "model: local2   # keep me" in u
    d = modelcfg.delete(u, 1)
    assert [e["model"] for e in modelcfg.entries(d)] == ["local2", "new"] and "# top comment" in d
    with pytest.raises(modelcfg.EditError):
        modelcfg.delete(d, 9)
    assert modelcfg.update(CFG, 0, modelcfg.entries(CFG)[0]) == CFG


def test_modelcfg_refuses_what_it_cannot_edit():
    with pytest.raises(modelcfg.EditError, match="valid YAML|tabs"):     # R556
        modelcfg.add("models:\n\t- model: x\n", {"model": "y"})
    with pytest.raises(modelcfg.EditError, match="tabs"):
        modelcfg.add("models: []\nx: 'a\tb'\n", {"model": "y"})
    with pytest.raises(modelcfg.EditError, match="inline"):
        modelcfg.add("models: [{model: x}]\n", {"model": "y"})
    t = modelcfg.add("runtime: {}\n", {"model": "a"})
    assert modelcfg.entries(t) == [{"model": "a"}]
    t = modelcfg.add("models: []\n", {"model": "a"})
    assert modelcfg.entries(t) == [{"model": "a"}]
    one = "models:\n  - {model: x, provider: y}  # c\n"
    assert "# c" in modelcfg.update(one, 0, {"model": "z", "provider": "y"})
