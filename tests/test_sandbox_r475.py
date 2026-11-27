"""R475/R476: the command sandbox, attacked for real.

Every test runs commands in the actual box (bubblewrap on Linux, sandbox-exec on macOS) against a fake home folder
with planted secrets, so nothing real is read or written. Skipped where the box cannot run (its own trial run fails)."""
import os
import socket
import subprocess
import threading

import pytest

from aurora import sandbox, tools


@pytest.fixture
def box(box_home_root, monkeypatch):
    root = box_home_root                     # macOS: not under $TMPDIR, which the box may write
    home, ah, ws = root / "home", root / "home" / ".aurora", root / "ws"
    for d in (home / ".ssh", home / ".cache", ah / "workspace" / "input", ah / "workspace" / "outputs", ah / "skills",
              ws / ".git" / "hooks", ws / "src"):
        d.mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    (home / ".bashrc").write_text("# rc\n")
    (home / ".netrc").write_text("machine x password hunter2\n")
    (ah / "config.yaml").write_text("api_key: sk-secret\n")
    (ah / "keys.salt").write_text("salt")
    (ah / "workspace" / "input" / "upload.txt").write_text("uploaded")
    (ah / "skills" / "hello").write_text("#!/bin/sh\necho hi\n")
    (ws / ".git" / "config").write_text("[core]\n")
    (ws / "src" / "a.py").write_text("print(1)\n")
    p = sandbox.Policy(home=str(home), aurora_home=str(ah))
    ok, why = sandbox.check(p)
    if not ok:
        pytest.skip(f"no sandbox here: {why}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(ws)

    def run(cmd, cwd=None, policy=p):
        a = sandbox.argv(policy, cmd, str(ws), "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh",
                         str(cwd or ws))
        r = subprocess.run(a, capture_output=True, text=True, timeout=30, cwd=str(cwd or ws),
                           env=sandbox.env({**tools._tool_env(), "HOME": str(home)}), stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr
    run.home, run.ah, run.ws, run.policy = home, ah, ws, p
    return run


# ── what must still work ─────────────────────────────────────────────────────

def test_the_workspace_uploads_outputs_and_cache_are_usable(box):
    code, out = box("echo new > src/b.py && cat src/a.py src/b.py && cat ~/.aurora/workspace/input/upload.txt"
                    " && echo made > ~/.aurora/workspace/outputs/o.txt && echo c > ~/.cache/x && cat ~/.bashrc")
    assert code == 0, out
    assert "print(1)" in out and "new" in out and "uploaded" in out and "# rc" in out
    assert (box.ws / "src" / "b.py").read_text() == "new\n"
    assert (box.ah / "workspace" / "outputs" / "o.txt").read_text() == "made\n"


def test_the_private_tmp_lasts_between_commands_and_is_not_the_hosts(box, tmp_path):
    if sandbox.platform_tool() != "bwrap":
        pytest.skip("bubblewrap only: sandbox-exec has no private /tmp (it keeps the per-user temp dir)")
    marker = f"/tmp/aurora-host-marker-{os.getpid()}"
    open(marker, "w").close()
    try:
        assert box("echo kept > /tmp/r475")[0] == 0
        code, out = box(f"cat /tmp/r475; ls {marker} 2>&1")
        assert "kept" in out and "No such file" in out
    finally:
        os.unlink(marker)


def test_a_skill_can_be_read_but_not_changed(box):
    code, out = box("cat ~/.aurora/skills/hello && echo x >> ~/.aurora/skills/hello")
    assert "echo hi" in out and code != 0
    assert box.ah.joinpath("skills", "hello").read_text() == "#!/bin/sh\necho hi\n"


# ── what must fail: secrets ─────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "cat ~/.ssh/id_ed25519", "cat ~/.netrc", "cat ~/.aurora/config.yaml", "cat ~/.aurora/keys.salt",
    "cat ~/.s?h/id_*", "find ~ -name 'id_ed25519' -exec cat {} +", "python3 -c \"print(open('$HOME/.ssh/id_ed25519').read())\"",
    "cd ~/.ssh && cat id_ed25519", "k=$HOME/.ss; cat ${k}h/id_ed25519", "tar cf - ~/.ssh 2>/dev/null | tar tvf - ",
    "grep -r hunter2 ~ 2>/dev/null", "grep -r sk-secret ~ 2>/dev/null"])
def test_secrets_cannot_be_read(box, cmd):
    code, out = box(cmd)
    assert "PRIVATE KEY" not in out and "hunter2" not in out and "sk-secret" not in out and "salt" != out.strip(), out


def test_etc_environment_is_empty_inside(box):
    if not os.path.exists("/etc/environment"):
        pytest.skip("no /etc/environment here")
    code, out = box("c=/etc; cat $c/environment | wc -c")
    assert out.strip().splitlines()[-1].strip() == "0" or code != 0, out


# ── what must fail: writes outside the box, and persistence ───────────────────

@pytest.mark.parametrize("cmd", [
    "echo evil >> ~/.bashrc", "echo evil > ~/.profile", "mkdir -p ~/.config/systemd/user && echo x > ~/.config/systemd/user/a.service",
    "echo '#!/bin/sh' > .git/hooks/post-commit", "echo '[core] fsmonitor = evil' >> .git/config",
    "ln -s ~/.bashrc link && echo evil >> link", "python3 -c \"open('$HOME/.bashrc','a').write('evil')\"",
    "cp /bin/sh ~/.local-sh", "echo evil > ~/.aurora/extensions.py", "echo x > /etc/aurora-evil", "touch /usr/aurora-evil"])
def test_writes_outside_the_workspace_fail(box, cmd):
    box(cmd)
    assert box.home.joinpath(".bashrc").read_text() == "# rc\n"
    assert not box.home.joinpath(".profile").exists() and not box.home.joinpath(".config", "systemd").exists()
    assert not box.ws.joinpath(".git", "hooks", "post-commit").exists()
    assert box.ws.joinpath(".git", "config").read_text() == "[core]\n"
    assert not box.home.joinpath(".local-sh").exists() and not box.ah.joinpath("extensions.py").exists()
    assert not os.path.exists("/etc/aurora-evil") and not os.path.exists("/usr/aurora-evil")


def test_the_cwd_argument_does_not_make_a_folder_writable(box):
    code, out = box("echo evil > planted", cwd=box.home)
    assert code != 0 and not box.home.joinpath("planted").exists()


# ── what must fail: ways out of the box ─────────────────────────────────────

def test_the_docker_socket_is_shut(box):
    s = next((x for x in sandbox.SOCKETS if os.path.exists(x) and os.access(x, os.W_OK)), None)
    if s is None:
        pytest.skip("no reachable docker socket on this machine")
    code, out = box(f"python3 -c \"import socket;c=socket.socket(socket.AF_UNIX);c.connect('{s}');print('CONN'+'ECTED')\"")
    assert "CONNECTED" not in out


def test_the_users_runtime_dir_is_empty(box):
    d = f"/run/user/{os.getuid()}"
    if not os.path.isdir(d) or not os.listdir(d):
        pytest.skip("no user runtime dir here")
    code, out = box(f"ls -A {d} | wc -l; systemctl --user status 2>&1 | head -1")
    assert out.splitlines()[0].strip() == "0", out


def test_host_processes_are_invisible(box):
    if sandbox.platform_tool() != "bwrap":
        pytest.skip("bubblewrap only: sandbox-exec has no PID namespace")
    code, out = box(f"kill -0 {os.getpid()} 2>&1 && echo ALIVE; ls /proc | grep -c '^[0-9]'")
    assert "ALIVE" not in out
    assert int(out.strip().splitlines()[-1]) < 10, out


def test_no_privilege_comes_back_inside(box):
    code, out = box("sudo -n true 2>&1; echo rc=$?; unshare -rm sh -c 'mount -o remount,rw / && echo REMOUNTED' 2>&1;"
                    " (command -v bwrap >/dev/null && bwrap --bind / / --dev /dev touch $HOME/.nested-escape 2>&1) ;"
                    " echo done")
    assert "rc=0" not in out and "REMOUNTED" not in out, out
    assert not box.home.joinpath(".nested-escape").exists()


def test_agent_and_bus_variables_are_dropped(box, monkeypatch):
    e = sandbox.env({"SSH_AUTH_SOCK": "/x", "DBUS_SESSION_BUS_ADDRESS": "unix:x", "TMUX": "t", "PATH": "/usr/bin"})
    assert "SSH_AUTH_SOCK" not in e and "DBUS_SESSION_BUS_ADDRESS" not in e and "TMUX" not in e
    assert e["PATH"] == "/usr/bin" and e["AURORA_SANDBOX"] == "1"


def test_network_off_means_no_connection(box):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()
    probe = f"python3 -c \"import socket;socket.create_connection(('127.0.0.1',{port}),2);print('CONN'+'ECTED')\""
    try:
        assert "CONNECTED" in box(probe)[1]                                    # on by default
        off = sandbox.Policy(home=box.policy.home, aurora_home=box.policy.aurora_home, network=False)
        assert "CONNECTED" not in box(probe, policy=off)[1]
    finally:
        srv.close()


# ── through the real tool ───────────────────────────────────────────────────

def test_run_command_uses_the_box_and_bash_mode_does_not(box):
    from aurora import bashmode
    try:
        assert tools.enable_sandbox(box.policy) == "" and tools.sandbox_active()
        out = tools.run_command("echo $AURORA_SANDBOX; cat ~/.ssh/id_ed25519 2>&1; echo x >> ~/.bashrc 2>&1")
        assert "1" in out.splitlines()[0] and "PRIVATE KEY" not in out
        assert box.home.joinpath(".bashrc").read_text() == "# rc\n"
        jid = tools.run_command("echo bg >> ~/.bashrc", background=True)
        for _ in range(50):
            if "running" not in tools.check_command(jid):
                break
            threading.Event().wait(0.1)
        assert box.home.joinpath(".bashrc").read_text() == "# rc\n"                 # a background job is boxed too
        res, _ = bashmode.run("echo ${AURORA_SANDBOX:-none}; cat ~/.ssh/id_ed25519",
                              bashmode.initial_state(str(box.ws)), carry=False)
        assert "none" in res.out and "PRIVATE KEY" in res.out                       # the user's own terminal
    finally:
        tools.disable_sandbox()


def test_argv_keeps_the_order_that_makes_hiding_win(box):
    a = sandbox._bwrap_args(box.policy, str(box.ws), str(box.ws)) if sandbox.platform_tool() == "bwrap" else None
    if a is None:
        pytest.skip("bubblewrap only")
    assert "--new-session" in a and "--die-with-parent" in a and "--unshare-pid" in a
    first_hide = a.index(str(box.home / ".ssh"))
    assert all(i < first_hide for i, x in enumerate(a) if x == "--bind" and a[i + 1] == str(box.ws))
    assert a.index(str(box.ws / ".git" / "hooks")) > a.index(str(box.ws))
