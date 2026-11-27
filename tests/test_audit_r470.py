"""R470-R477: second round of the 2026-11-24 audit (the open findings, then web approval and shell isolation)."""
import http.client
import json

from aurora.web import server as web_server
from tests.test_web import app, call  # noqa: F401  (fixture)


def _raw(app, path, body, headers=None):
    host, port = app.base.split("//")[1].split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=5)
    c.request("POST", path, body=body, headers={"Authorization": "Bearer t0k", "Content-Type": "application/json",
                                                **(headers or {})})
    r = c.getresponse()
    return r.status, r.read()


# ── R470: /api/files/delete_all answers an oversized body with 413 ───────────

def test_r470_delete_all_oversized_body_is_a_413(app):
    code, raw = _raw(app, "/api/files/delete_all", b"x" * (web_server.MAX_BODY + 10))
    assert code == 413 and "too large" in json.loads(raw)["error"]


def test_r470_delete_all_kind_of_the_wrong_type_is_a_400(app):
    assert call(app, "/api/files/delete_all", {"kind": ["input"]})[0] == 400
    assert call(app, "/api/files/delete_all", {"kind": {"a": 1}})[0] == 400


# ── R471: the offline cache keeps the 20 most recently read transcripts, never a deleted chat ───────────

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _sw(steps):
    out = subprocess.run(["node", str(ROOT / "tests/js/sw_harness.js"), str(ROOT / "aurora/web/sw.js"), json.dumps(steps)],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


@needs_node
def test_r471_only_the_most_recent_transcripts_are_kept():
    steps = [{"path": f"/api/sessions/s{i:03d}"} for i in range(30)] + [{"path": "/api/sessions/s005"}]
    keys = _sw(steps)
    assert len(keys) == 20 and keys[-1] == "/api/sessions/s005"          # read again: newest, not evicted
    assert "/api/sessions/s011" in keys and "/api/sessions/s010" not in keys


@needs_node
def test_r471_a_deleted_chat_leaves_the_cache():
    keys = _sw([{"path": "/api/sessions/aaa"}, {"path": "/api/sessions/bbb"}, {"path": "/api/sessions"},
                {"method": "POST", "path": "/api/chat/delete", "body": {"id": "aaa"}}])
    assert keys == ["/api/sessions/bbb"]                                  # the chat and the stale chat list
    keys = _sw([{"path": "/api/sessions/aaa"}, {"path": "/api/sessions/bbb"},
                {"method": "POST", "path": "/api/folder/delete", "body": {"id": "f1"}}])
    assert keys == []
    keys = _sw([{"path": "/api/sessions/aaa"}, {"path": "/api/sessions/aaa", "status": 404}])
    assert keys == []                                                     # gone on the server
    keys = _sw([{"path": "/api/sessions/aaa"}, {"method": "POST", "path": "/api/chat/delete", "body": {"id": "aaa"},
                                                "status": 400}])
    assert keys == ["/api/sessions/aaa"]                                  # a refused delete keeps it


# ── R472: a NUL in chat text cannot break or rewrite the export ───────────────

def test_r472_nul_in_text_is_shown_not_used_as_a_placeholder():
    from aurora.web import export
    assert export._inline("a \x007\x00 b") == "a �7� b"
    assert export._inline("`x` and \x000\x00") == "<code>x</code> and �0�"
    assert "<li>" in export.markdown("- item \x003\x00")


# ── R473: the web denylist reads paths from the directory the command really runs in ───────────

BYPASSES = ["cd /etc && cat environment", "cd /etc;cat environment", "cat /etc/./environment", "cat /etc//environment",
            "cat /etc/../etc/environment", "cd / && cd etc && cat environment", "pushd /etc >/dev/null; head environment",
            "cd /proc/self && cat environ", "(cd /etc && cat environment)", "cd /etc\ncat environment",
            "cd ../../../../../../../../etc && cat environment", "cd ~ && cd .ss''h && cat id_ed25519"]


@pytest.mark.parametrize("cmd", BYPASSES)
def test_r473_cd_and_dot_paths_do_not_step_round_the_denylist(cmd):
    from aurora.web import core
    assert core.web_denied("run_command", {"command": cmd}, list(core.WEB_DENY_DEFAULT)), cmd


def test_r473_the_cwd_argument_counts_too():
    from aurora.web import core
    d = list(core.WEB_DENY_DEFAULT)
    assert core.web_denied("run_command", {"command": "cat environment", "cwd": "/etc"}, d)
    assert core.web_denied("wait_until", {"command": "test -s environment", "cwd": "/etc"}, d)


@pytest.mark.parametrize("cmd", ["cd /tmp && ls", "cat README.md", "cd docs && cat environment.md", "ls -la",
                                 "python -m pytest -q", "cd $HOME && ls", "git log --oneline -5"])
def test_r473_ordinary_commands_still_pass(cmd):
    from aurora.web import core
    assert not core.web_denied("run_command", {"command": cmd}, list(core.WEB_DENY_DEFAULT)), cmd


# ── R474: the web page asks before every write and command until auto-approve is turned on ───────────

def test_r474_web_asks_by_default(app, monkeypatch):
    from aurora import service
    from aurora.web.core import WebSession
    monkeypatch.setattr(service, "load_state", lambda: {})
    assert WebSession(app.e).auto is False
    monkeypatch.setattr(service, "load_state", lambda: {"web_auto_approve": "yes"})
    assert WebSession(app.e).auto is False                       # only a real True turns it on
    monkeypatch.setattr(service, "load_state", lambda: {"web_auto_approve": True})
    assert WebSession(app.e).auto is True                        # the page's own switch still survives a restart
    monkeypatch.setattr(service, "load_state", lambda: (_ for _ in ()).throw(OSError("bad state")))
    assert WebSession(app.e).auto is False                       # an unreadable state file never means "run freely"


def test_r474_a_command_waits_for_the_page_by_default(app, monkeypatch):
    from aurora import service
    from aurora.web.core import WebSession
    monkeypatch.setattr(service, "load_state", lambda: {})
    s = WebSession(app.e, approve_timeout=0.2)
    asked = []
    monkeypatch.setattr(s, "ask", lambda tool, args, diff: (asked.append(tool), "n")[1])
    from aurora.web.core import WebFrontend
    assert WebFrontend(s).approve("run_command", {"command": "touch x"}, "") == "n" and asked == ["run_command"]
