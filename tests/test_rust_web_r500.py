"""R500 — the web page's safety on the Rust core: the path guard (R450) answers
as `aurora/guard.py` does, and a web turn runs on the Rust core with the guard
on, so a prompt-injected read of a secret is refused the same way."""

import json
import os
import subprocess
import sys

import pytest

from tests.test_rust_compat_r482 import _Model, _records, _setup, _sse, _tool_call, aurora_rs  # noqa: F401


@pytest.fixture
def tree(tmp_path):
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    (tmp_path / "proj" / ".ssh").mkdir(parents=True)
    (tmp_path / "proj" / "ok.txt").write_text("x")
    (tmp_path / "proj" / "link").symlink_to(tmp_path / "proj" / ".ssh")
    return tmp_path, home


def _cases(tmp, home):
    paths = [".", "ok.txt", "~/.ssh/id_rsa", "/proc/self/environ", "/proc/1/task/2/environ", "/etc/shadow",
             "/etc/sudoers.d/x", "/etc/passwd", "a/../.aws/credentials", "x.TOKEN", "srv.pem", "my.key", "keys.enc",
             ".git-credentials", "link/id", str(home / "config.yaml"), str(home / "workspace" / "out.txt"),
             str(home / "denylist.yaml"), str(home), "", "nested/.gnupg/x"]
    tools = ["read_file", "list_dir", "grep", "find_files", "write_file", "edit_file", "apply_patch", "run_command"]
    cases = [{"tool": t, "args": {"path": p}} for t in tools for p in paths]
    return cases + [{"tool": "read_file", "args": {}}, {"tool": "write_file", "args": {"path": 3}}]


def test_guard_matches_python(aurora_rs, tree, monkeypatch):
    tmp, home = tree
    cases = _cases(tmp, home)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AURORA_")}
    env["AURORA_HOME"] = str(home)
    p = subprocess.run([aurora_rs, "--guard-oracle"], input=json.dumps(cases), capture_output=True, text=True,
                       env=env, cwd=tmp / "proj", timeout=60)
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout)
    monkeypatch.setenv("AURORA_HOME", str(home))
    monkeypatch.chdir(tmp / "proj")
    from aurora import guard
    monkeypatch.setattr(guard, "WEB", True)
    want = [guard.check(c["tool"], c["args"]) for c in cases]
    bad = [(c, g, w) for c, g, w in zip(cases, got["checks"], want) if g != w]
    assert not bad, f"{len(bad)} of {len(cases)} differ; first: {bad[0]}"
    assert got["grep_excludes"] == guard.grep_excludes()
    assert sum(bool(w) for w in want) > 40


def test_a_web_turn_on_the_rust_core_refuses_a_secret_read(aurora_rs, tmp_path, monkeypatch):
    """The web page's WebSession on core: rust — the core starts with --web and the guard refuses."""
    got = {}
    for core in ("python", "rust"):
        model = _Model([_tool_call("read_file", {"path": "~/.ssh/id_rsa"}, "c1"), _sse({"content": "ok"})])
        try:
            home, work, cfg, env = _setup(tmp_path / core[:2], model.url)
            cfg.write_text(cfg.read_text() + f"  core: {core}\n")
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            monkeypatch.setenv("AURORA_RS", aurora_rs)
            monkeypatch.delenv("AURORA_CORE", raising=False)        # this test picks each core itself
            monkeypatch.chdir(work)
            from aurora.engine import Engine
            from aurora.web.core import WebSession
            e = Engine(str(cfg))
            s = WebSession(e, approve_timeout=0.5, auto_approve=False)
            from tests.test_rust_core_r499 import _FE
            e.send("read my key", _FE("n"))
            assert (core == "rust") == (getattr(e, "_rust_core", None) is not None)
            if getattr(e, "_rust_core", None):
                assert e._rust_core.key[2] is True          # started with --web
                e._rust_core.close()
            del s
        finally:
            model.close()
            from aurora import service
            service.set_web_guard(False)
        _sid, recs, _ = _records(home)
        got[core] = [r.get("output") for r in recs if r["event"] == "tool"]
    assert got["rust"] == got["python"]
    assert got["rust"][0].startswith("[blocked on the web page: ~/.ssh/id_rsa (key/credential folder)]")


def test_a_web_turn_on_the_rust_core_boxes_commands(aurora_rs, tmp_path, monkeypatch, box_home_root):
    """R475: the web page boxes commands by default (`sandbox: web`); the Rust core with --web does too."""
    import shutil
    if not (shutil.which("bwrap") or (sys.platform == "darwin" and shutil.which("sandbox-exec"))):
        pytest.skip("no command box on this machine")
    from tests.test_rust_compat_r482 import _cmd_call
    from tests.test_rust_core_r499 import _FE
    seen = {}
    for core in ("python", "rust"):
        model = _Model([_cmd_call("echo ok > in_ws.txt; echo x > ~/escape.txt; cat ~/.ssh/id_x"), _sse({"content": "done"})])
        try:
            home, work, cfg, env = _setup(tmp_path / core[:2], model.url)
            fake_home = box_home_root / core[:2] / "uhome"
            (fake_home / ".ssh").mkdir(parents=True)
            (fake_home / ".ssh" / "id_x").write_text("PRIVATE")
            env["HOME"] = str(fake_home)
            cfg.write_text(cfg.read_text().replace("sandbox: off", "sandbox: web") + f"  core: {core}\n")
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            monkeypatch.setenv("AURORA_RS", aurora_rs)
            monkeypatch.delenv("AURORA_CORE", raising=False)        # this test picks each core itself
            monkeypatch.chdir(work)
            from aurora import service
            from aurora.engine import Engine
            from aurora.web.core import WebSession
            e = Engine(str(cfg))
            problem = service.web_sandbox(e.runtime)            # what `aurora --web` does at start
            if problem:
                pytest.skip(f"the box cannot run here: {problem}")
            WebSession(e, approve_timeout=0.5, auto_approve=False)
            e.send("go", _FE("y"))
            assert (core == "rust") == (getattr(e, "_rust_core", None) is not None)
            if getattr(e, "_rust_core", None):
                e._rust_core.close()
        finally:
            model.close()
            from aurora import service, tools
            service.set_web_guard(False)
            tools.disable_sandbox()
        result = json.dumps(model.requests[-1]["messages"][-1])
        seen[core] = ((work / "in_ws.txt").exists(), (fake_home / "escape.txt").exists(), "PRIVATE" in result)
    assert seen["python"] == seen["rust"] == (True, False, False), seen
