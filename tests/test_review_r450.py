"""R450 — fixes from the Aurora issues card 20261114-203412 (S1-S11, B1-B3, B5, P1, P3)."""
import io
import os
import time
from pathlib import Path
import zipfile

import pytest

from aurora import approve, guard, rewind, secrets, tools
from aurora.extensions_bundled import web_extension as we
from aurora.web import attach
from aurora.web import core as web_core


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "workspace").mkdir(parents=True)
    monkeypatch.setenv("AURORA_HOME", str(h))
    return h


# ── S6 / B2: html to text is linear ──────────────────────────────────────────
@pytest.mark.parametrize("pay", ["<!-- " * 60000, "<a " * 80000, "<p x" * 80000, "<li " * 60000,
                                 "<script></script>" * 40000 + "x", "<" * 100000])
def test_s6_html_to_text_is_fast_on_unclosed_markup(pay):
    t = time.time()
    we.html_to_text(pay)
    assert time.time() - t < 2


def test_s6_html_to_text_still_reads_a_page():
    page = "<main>" + "x" * 300 + "<ul><li>a</li><li>b<br>c</ul><!-- note --><script>bad()</script><p>q</p></main>"
    out = we.html_to_text(page)
    assert "- a" in out and "- b\nc" in out and "q" in out and "note" not in out and "bad()" not in out


@pytest.mark.parametrize("pay", [b"<script " * 60000, b"<a " * 80000, b"<!-- " * 60000])
def test_b2_html_attachment_is_fast(pay):
    t = time.time()
    attach._html(pay)
    assert time.time() - t < 2


def test_b2_html_attachment_text():
    assert attach._html(b"<p>a&amp;b</p><script>x</script><li>c<br>d").split() == ["a&b", "-", "c", "d"]


# ── S1 / S8: the agent cannot edit policy, state or code in AURORA_HOME ──────
@pytest.mark.parametrize("rel", ["web-denylist.txt", "allowlist.yaml", "denylist.yaml", "state.yaml",
                                 "extensions/x.py", "bootstrap.md", "sessions/a.jsonl"])
def test_s1_s8_web_cannot_write_policy_files(home, rel):
    guard.WEB = True
    for tool in ("write_file", "edit_file", "apply_patch"):
        out = tools.run_tool(tool, {"path": str(home / rel), "content": "", "old": "a", "new": "b", "diff": ""})
        assert out.startswith("[blocked on the web page"), (tool, rel, out)
    assert not (home / rel).exists()


def test_s8_workspace_stays_writable_and_terminal_unchanged(home):
    guard.WEB = True
    assert "wrote" in tools.run_tool("write_file", {"path": str(home / "workspace/outputs-x.txt"), "content": "hi"})
    guard.WEB = False
    assert "wrote" in tools.run_tool("write_file", {"path": str(home / "state.yaml"), "content": "a: 1\n"})


def test_s8_symlink_into_home_is_followed(home, tmp_path):
    link = tmp_path / "ws-link"
    link.symlink_to(home / "allowlist.yaml")
    guard.WEB = True
    assert tools.run_tool("write_file", {"path": str(link), "content": "x"}).startswith("[blocked")


def test_s1_empty_denylist_file_means_defaults(home):
    (home / "web-denylist.txt").write_text("# nothing here\n")
    assert web_core.web_denylist() == list(web_core.WEB_DENY_DEFAULT)
    assert web_core.web_denied("run_command", {"command": "sudo id"})


# ── S2: secrets are not readable in web mode ─────────────────────────────────
@pytest.mark.parametrize("path", ["/proc/self/environ", "/etc/environment", "~/.ssh/id_ed25519", "~/.aws/credentials",
                                  "/etc/shadow", "/tmp/x/claude-code.token", "/tmp/x/k.pem"])
@pytest.mark.parametrize("tool", ["read_file", "list_dir", "grep", "find_files"])
def test_s2_web_cannot_read_secret_paths(path, tool):
    guard.WEB = True
    out = tools.run_tool(tool, {"path": path, "pattern": "a"})
    assert out.startswith("[blocked on the web page"), out


@pytest.mark.parametrize("path", ["/private/etc/shadow", "/private/etc/environment", "/etc/master.passwd", "/private/etc/sudoers.d/x"])
def test_s2_macos_etc_paths_are_secret_too(path):
    """N1: on macOS /etc resolves to /private/etc; the guard must still see a credential file."""
    from pathlib import Path
    assert guard.secret_reason(Path(path)) == "credential file"
    assert guard.secret_reason(Path("/private/etc/hosts")) == ""


def test_s2_ordinary_files_and_terminal_still_read(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    guard.WEB = True
    assert "hello" in tools.run_tool("read_file", {"path": str(f)})
    guard.WEB = False
    assert guard.check("read_file", {"path": "/proc/self/environ"}) == ""


def test_s2_grep_over_a_parent_skips_secret_folders(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id").write_text("NEEDLE-secret")
    (tmp_path / "ok.txt").write_text("NEEDLE-fine")
    guard.WEB = True
    out = tools.run_tool("grep", {"pattern": "NEEDLE", "path": str(tmp_path)})
    assert "NEEDLE-fine" in out and "NEEDLE-secret" not in out


def test_s2_redactor_sees_nul_separated_environ():
    text = "A=1\0AURORA_WEB_TOKEN=abcdef123456\0B=2"
    assert any("AURORA_WEB_TOKEN" in m.text for m in secrets.scan(text))


def test_b5_names_ending_in_pass_are_redacted():
    assert secrets.scan("MYAPP_DBPASS=hunter22x\n")
    assert not secrets.scan("PASSENGER=x\n")


# ── S3: a flagged fetch is always put to the page ────────────────────────────
def test_s3_exfil_fetch_not_auto_approved():
    asked = []

    class S:
        auto = True

        def emit(self, *a, **k):
            pass

        def ask(self, tool, args, diff):
            asked.append(tool)
            return "n"

    fe = web_core.WebFrontend.__new__(web_core.WebFrontend)
    fe.s = S()
    fe.flush = lambda: None
    assert fe.approve("web_fetch", {"url": "https://example.com/?q=secret"}, "") == "n"
    assert asked == ["web_fetch"]
    assert fe.approve("web_fetch", {"url": "https://example.com/page"}, "") == "y"


# ── B1 / S4: the denylist matches what is run, not what is written ───────────
@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "/tmp/a.py", "content": "import subprocess\nsubprocess.run(['docker', 'ps'])"}),
    ("write_file", {"path": "/tmp/README.md", "content": "run `sudo` and `ssh` then git push"}),
    ("edit_file", {"path": "/tmp/a.py", "old": "eval(x)", "new": "docker"}),
])
def test_b1_file_content_is_not_scanned(tool, args):
    assert not web_core.web_denied(tool, args, None)


def test_b1_file_path_is_scanned():
    assert web_core.web_denied("write_file", {"path": "/etc/environment", "content": "x"}, None)


@pytest.mark.parametrize("cmd", [
    "python3 -c \"import os; os.popen('id')\"", "perl -e 'system(\"id\")'", "echo $(echo c3VkbyBpZA== | base64 --decode)",
    "cat ~/.aurora/web-denylist.txt", "echo x > ~/.aurora/allowlist.yaml", "echo x >> state.yaml",
])
def test_s4_more_evasions_denied(cmd):
    assert web_core.web_denied("run_command", {"command": cmd}, None), cmd


def test_s4_globs_are_expanded(tmp_path, monkeypatch):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("k")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert web_core.web_denied("run_command", {"command": "cat ~/.s?h/id_ed25519"}, None)
    if os.path.exists("/etc/environment"):         # Linux; macOS has no such file, so the glob names nothing
        assert web_core.web_denied("run_command", {"command": "cat /etc/environ*"}, None)


def test_s4_nested_mcp_arguments_are_scanned():
    assert web_core.web_denied("mcp_x_run", {"opts": {"cmd": ["sudo", "id"]}}, None)
    assert not web_core.web_denied("mcp_x_run", {"opts": {"cmd": ["ls"]}}, None)


# ── S7: an "always allow" never covers a program or a file write ─────────────
def _rule_then(first, later):
    tool = "run_command"
    rule = approve.add_rule(tool, {"command": first}) if False else approve._rule_for(tool, {"command": first})
    return approve._matches(tool, {"command": later}, {tool: [rule]}, strict=True)


@pytest.mark.parametrize("first,later", [
    ("git grep TODO", "git grep -O'rm x' TODO"),
    ("git grep TODO", "git grep --open-files-in-pager=cmd TODO"),
    ("git diff HEAD", "git diff --output=/home/u/.bashrc"),
    ("git log --oneline", "git log --output=x"),
    ("git fetch origin", "git fetch --upload-pack=cmd origin"),
    ("git ls-remote origin", "git ls-remote --upload-pack=cmd origin"),
    ("git submodule status", "git submodule foreach id"),
    ("git bisect log", "git bisect run id"),
    ("git rebase --continue", "git rebase --exec id main"),
    ("git difftool main", "git difftool --extcmd=id main"),
    ("git archive HEAD", "git archive --output=/tmp/x HEAD"),
    ("flock x ls", "flock x reboot"),
    ("gh pr list", "gh pr merge 3"),
    ("gh api repos/x", "gh api -X DELETE repos/x"),
    ("zip -r a.zip src", "zip -r a.zip src -TT 'id'"),
])
def test_s7_always_allow_does_not_widen(first, later):
    assert not _rule_then(first, later), (first, later)


@pytest.mark.parametrize("first,later", [
    ("git status", "git status --short"), ("git log --oneline", "git log --oneline"),
    ("git grep TODO", "git grep FIXME"), ("git diff HEAD", "git diff main"),
])
def test_s7_ordinary_git_still_generalises(first, later):
    assert _rule_then(first, later)


# ── B3 / P1 ──────────────────────────────────────────────────────────────────
def test_b3_files_listing_survives_a_vanishing_file(home, monkeypatch):
    d = attach.folder("input")
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.txt").write_text("a")
    (d / "b.txt").write_text("b")
    real = type(d / "a.txt").stat

    def flaky(self, *a, **k):
        if self.name == "b.txt":
            raise FileNotFoundError(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(type(d / "a.txt"), "stat", flaky)
    assert [f["name"] for f in attach.files("input")] == ["a.txt"]


def test_p1_office_file_total_size_is_capped(monkeypatch):
    monkeypatch.setattr(attach, "MAX_ZIP_TOTAL", 5000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(1, 8):
            z.writestr(f"xl/worksheets/sheet{i}.xml", "<w>" + "a" * 2000 + "</w>")
    with pytest.raises(attach.Unsupported):
        attach._xlsx(buf.getvalue())


# ── S5 / S9 / S10 / S11 ─────────────────────────────────────────────────────
def test_s5_handler_has_a_socket_timeout():
    from aurora.web import server
    assert server.SOCKET_TIMEOUT_S > server.HEARTBEAT_S
    h = server.make_handler(None, "t")
    assert h.timeout == server.SOCKET_TIMEOUT_S


def test_s9_more_credentials_are_not_inherited(monkeypatch):
    for k in ("SSH_AUTH_SOCK", "DATABASE_URL", "REPORTS_DSN", "GH_PAT"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("HARMLESS", "keep")
    env = tools._tool_env()
    assert not {"SSH_AUTH_SOCK", "DATABASE_URL", "REPORTS_DSN", "GH_PAT"} & set(env)
    assert env["HARMLESS"] == "keep"


def test_s10_home_folder_is_private(tmp_path, monkeypatch):
    h = tmp_path / "h"
    h.mkdir(mode=0o775)
    h.chmod(0o775)
    monkeypatch.setenv("AURORA_HOME", str(h))
    from aurora import paths
    paths.aurora_home()
    assert h.stat().st_mode & 0o077 == 0


def test_s11_page_clears_offline_data_on_token_change():
    js = (Path(attach.__file__).parent / "app.js").read_text()
    assert "clearOfflineData" in js and "startsWith('/api/')" in js
    assert "if (v !== getToken()) await clearOfflineData()" in js


# ── P3 ───────────────────────────────────────────────────────────────────────
def _fake_repo(base, name, folder, age_days):
    gd = base / name
    gd.mkdir(parents=True)
    (gd / "HEAD").write_text("ref: refs/heads/master\n")
    if folder is not None:
        (gd / "aurora-cwd").write_text(str(folder))
    t = time.time() - age_days * 86400
    for f in (gd / "HEAD", gd / "aurora-cwd", gd):
        if f.exists():
            os.utime(f, (t, t))
    return gd


def test_p3_sweep_removes_repos_with_no_folder_or_too_old(home, tmp_path):
    base = home / "checkpoints"
    live = tmp_path / "live"
    live.mkdir()
    keep = _fake_repo(base, "a" * 16, live, 1)
    gone = _fake_repo(base, "b" * 16, tmp_path / "deleted", 1)
    old = _fake_repo(base, "c" * 16, live, 120)
    legacy_old = _fake_repo(base, "d" * 16, None, 120)
    legacy_new = _fake_repo(base, "e" * 16, None, 2)
    stray = base / "notes"
    stray.mkdir()
    assert rewind.sweep_stale() == 3
    assert keep.exists() and legacy_new.exists() and stray.exists()
    assert not gone.exists() and not old.exists() and not legacy_old.exists()


def test_p3_checkpoint_writes_the_folder_mark(home, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "f.txt").write_text("1")
    rewind.checkpoint("t", str(proj))
    assert (rewind._gitdir(proj.resolve()) / "aurora-cwd").read_text() == str(proj.resolve())
