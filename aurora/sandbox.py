"""R475: the sandbox the model's shell commands run in.

A command the model runs (`run_command`, `wait_until`, a background job, an
executable skill) gets the machine read-only, except for the workspace (the
directory Aurora works in), Aurora's own `workspace/` folder (uploads and
outputs), `~/.cache` and a private `/tmp`. The places where secrets live and
the ways out of the box are hidden:

- `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.docker`, `~/.kube`, `~/.password-store`,
  `~/.netrc`, `~/.git-credentials`, `/etc/environment`, and Aurora's own home
  (keys, config, extensions, sessions) apart from `workspace/` and `skills/`;
- the Docker socket, the user's runtime dir (`/run/user/<uid>`: D-Bus,
  `systemd --user`, the ssh and gpg agents) and the host's `/tmp` (tmux and X
  sockets), so a command cannot start a process outside the box;
- `.git/hooks` and `.git/config` in the workspace stay read-only, so a command
  cannot leave a hook that runs, unsandboxed, the next time you use git.

Linux uses bubblewrap (`bwrap`) with its own PID, IPC and UTS namespaces and a
new session (no keystrokes pushed into the terminal); macOS uses
`sandbox-exec` with an equivalent profile. Before the first command the box is
checked by a real trial run (a write in the workspace works, a write in the
home folder and a read of a hidden folder fail). A machine where the check
fails has no sandbox, and the caller decides what that means (the web page then
asks before every command, see `aurora/web/core.py`).

Commands you type yourself in bash mode (`!`) are never boxed: it is your
terminal.

Config (`runtime:` in config.yaml):
  sandbox: web          # web (default): commands from the web page; all: the terminal's too; off
  sandbox_network: true # false: no network inside the box (only loopback is gone too)
  sandbox_writable: []  # more folders the box may write (e.g. ~/repositories)
  sandbox_hide: []      # more files or folders the box must not see
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("web", "all", "off")

HIDDEN_HOME = (".ssh", ".gnupg", ".aws", ".docker", ".kube", ".password-store", ".netrc", ".git-credentials",
               ".config/gcloud", ".config/gh", ".azure", ".pgpass", ".my.cnf")
HIDDEN_ABS = ("/etc/environment",)
SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock", "/run/podman/podman.sock")
# Variables that point a command at a way out of the box (they would only fail inside, but say nothing of it)
DROP_ENV = ("SSH_AUTH_SOCK", "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "DOCKER_HOST",
            "TMUX", "TMUX_PANE", "STY", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")


@dataclass
class Policy:
    """Where the box may write and what it must not see. Paths are absolute."""
    home: str
    aurora_home: str
    network: bool = True
    writable: list[str] = field(default_factory=list)
    hide: list[str] = field(default_factory=list)

    def tmp_dir(self) -> str:
        """The box's own /tmp: kept between commands (a model writes /tmp/x in one call and reads it in the next)."""
        d = os.path.join(self.aurora_home, "sandbox", "tmp")
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        return d

    def writable_dirs(self, workspace: str) -> list[str]:
        ws = [workspace, os.path.join(self.aurora_home, "workspace"), os.path.join(self.home, ".cache"), *self.writable]
        out = []
        for d in ws:
            d = os.path.realpath(os.path.expanduser(d))
            if os.path.isdir(d) and d not in out and d != "/" and d != os.path.realpath(self.home):
                out.append(d)
        return out

    def hidden(self) -> list[str]:
        hs = [os.path.join(self.home, h) for h in HIDDEN_HOME] + list(HIDDEN_ABS) + [
            os.path.expanduser(h) for h in self.hide]
        return [os.path.realpath(h) for h in hs if os.path.lexists(h)]


# ── Linux: bubblewrap ────────────────────────────────────────────────────────

def _bwrap_args(p: Policy, workspace: str, chdir: str) -> list[str]:
    """Order matters: a later mount covers an earlier one. So the private /tmp and the writable folders come first, then everything hidden
    (a writable folder that holds ~/.ssh does not bring it back), then Aurora's workspace/ and skills/ come back, and
    last the .git files that must stay read-only."""
    a = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
         "--unshare-cgroup-try", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    if not p.network:
        a.append("--unshare-net")
    a += ["--bind", p.tmp_dir(), "/tmp"]       # first: a workspace under the host's /tmp is bound back on top of it
    home = os.path.realpath(p.home)
    if home == "/tmp" or home.startswith("/tmp/"):
        a += ["--ro-bind", home, home]          # a home folder under /tmp (tests, odd setups) stays readable
    rw = p.writable_dirs(workspace)
    for d in rw:
        a += ["--bind", d, d]
    run_user = f"/run/user/{os.getuid()}"
    if os.path.isdir(run_user):
        a += ["--tmpfs", run_user]
    for s in SOCKETS:
        if os.path.lexists(s) and os.access(s, os.W_OK):   # one this user could talk to (a root-only one is shut anyway)
            a += ["--ro-bind", "/dev/null", s]
    empty = _empty_file(p)
    for h in p.hidden():
        a += ["--tmpfs", h] if os.path.isdir(h) else ["--ro-bind", empty, h]
    ah = os.path.realpath(p.aurora_home)
    if os.path.isdir(ah):
        a += ["--tmpfs", ah]                    # keys, config, sessions, extensions: gone ...
        sk, wk = os.path.join(ah, "skills"), os.path.join(ah, "workspace")
        if os.path.isdir(sk):
            a += ["--ro-bind", sk, sk]          # ... apart from the skills (run, not changed)
        if os.path.isdir(wk):
            a += ["--bind", wk, wk]             # ... and uploads / outputs
    for d in rw:
        for g in (".git/hooks", ".git/config"):
            f = os.path.join(d, g)
            if os.path.exists(f):
                a += ["--ro-bind", f, f]
    a += ["--setenv", "TMPDIR", "/tmp", "--chdir", chdir]
    return a


def _empty_file(p: Policy) -> str:
    f = os.path.join(p.aurora_home, "sandbox", "empty")
    if not os.path.exists(f):
        os.makedirs(os.path.dirname(f), mode=0o700, exist_ok=True)
        open(f, "w").close()
        os.chmod(f, 0o400)
    return f


# ── macOS: sandbox-exec ──────────────────────────────────────────────────────

def _sb_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_profile(p: Policy, workspace: str) -> str:
    """The sandbox-exec profile. In SBPL the last matching rule wins, so: allow everything, deny writes and the hidden
    places, then allow the writable folders back."""
    real = os.path.realpath
    tmp = real(p.tmp_dir())
    ah = real(p.aurora_home)
    w = [real(d) for d in p.writable_dirs(workspace)] + [tmp]
    darwin_tmp = real(tempfile.gettempdir())     # /private/var/folders/..: the per-user temp dir many tools need
    lines = ["(version 1)", "(allow default)",
             '(deny file-write* (subpath "/"))',
             "(allow file-write* " + " ".join(f"(subpath {_sb_str(d)})" for d in [*w, darwin_tmp])
             + ' (literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper") (regex #"^/dev/tty")'
             + ' (regex #"^/dev/fd/"))']
    hidden = p.hidden() + [ah]
    lines.append("(deny file-read* file-write* " + " ".join(f"(subpath {_sb_str(h)})" for h in hidden) + ")")
    back_ro = [os.path.join(ah, "skills")]
    back_rw = [os.path.join(ah, "workspace")]
    lines.append("(allow file-read* " + " ".join(f"(subpath {_sb_str(d)})" for d in back_ro + back_rw) + ")")
    lines.append("(allow file-write* " + " ".join(f"(subpath {_sb_str(d)})" for d in back_rw) + ")")
    protect = [os.path.join(d, g) for d in w for g in (".git/hooks", ".git/config")]
    lines.append("(deny file-write* " + " ".join(f"(subpath {_sb_str(f)})" for f in protect) + ")")
    socks = [s for s in SOCKETS] + [os.path.join(p.home, ".docker/run/docker.sock")]
    lines.append("(deny network-outbound " + " ".join(
        f"(remote unix-socket (path-literal {_sb_str(s)}))" for s in socks) + ")")
    if not p.network:
        lines.append('(deny network-outbound (remote ip))')
    return "\n".join(lines) + "\n"


# ── the wrapper ──────────────────────────────────────────────────────────────

def platform_tool() -> str | None:
    if sys.platform.startswith("linux"):
        return "bwrap" if shutil.which("bwrap") else None
    if sys.platform == "darwin":
        return "sandbox-exec" if shutil.which("sandbox-exec") else None
    return None


def argv(p: Policy, command: str, workspace: str, shell: str | None, chdir: str | None = None) -> list[str]:
    """The argv that runs `command` (a shell string) inside the box. `workspace` is what it may write (Aurora's
    working directory, never the command's own `cwd` argument, which the model chooses); `chdir` is where it starts."""
    sh = shell or "/bin/sh"
    tool = platform_tool()
    if tool == "bwrap":
        return [*_bwrap_args(p, workspace, chdir or workspace), sh, "-c", command]
    if tool == "sandbox-exec":           # the caller's Popen(cwd=) sets where it starts
        return ["sandbox-exec", "-p", macos_profile(p, workspace), sh, "-c", command]
    raise RuntimeError("no sandbox tool on this machine")


def env(base: dict) -> dict:
    e = {k: v for k, v in base.items() if k not in DROP_ENV}
    e["TMPDIR"] = "/tmp" if platform_tool() == "bwrap" else base.get("TMPDIR", "/tmp")
    e["AURORA_SANDBOX"] = "1"
    return e


_PROBE = r"""
set -u
ws=$1; hid=$2
echo probe > "$ws/.aurora-sandbox-probe" && rm -f "$ws/.aurora-sandbox-probe" || { echo "workspace not writable"; exit 1; }
if ( echo x > "$HOME/.aurora-sandbox-escape" ) 2>/dev/null; then rm -f "$HOME/.aurora-sandbox-escape"; echo "home writable"; exit 1; fi
if ls "$hid" >/dev/null 2>&1 && [ -n "$(ls -A "$hid" 2>/dev/null)" ]; then echo "hidden folder readable"; exit 1; fi
echo ok
"""

_checked: dict[tuple, tuple[bool, str]] = {}
_lock = threading.Lock()


def check(p: Policy) -> tuple[bool, str]:
    """(works, why not): a real trial run of the box on this machine, once per policy per process."""
    key = (p.home, p.aurora_home, p.network, tuple(p.writable), tuple(p.hide), platform_tool())
    with _lock:
        if key in _checked:
            return _checked[key]
    res = _probe(p)
    with _lock:
        _checked[key] = res
    return res


def _probe(p: Policy) -> tuple[bool, str]:
    tool = platform_tool()
    if tool is None:
        return False, ("bubblewrap is not installed (apt install bubblewrap)" if sys.platform.startswith("linux")
                       else "no sandbox tool on this system")
    probe_ws = tempfile.mkdtemp(prefix="aurora-sbx-probe-")
    hidden_dir = tempfile.mkdtemp(prefix="aurora-sbx-hidden-")
    try:
        Path(hidden_dir, "secret").write_text("x")
        q = Policy(home=p.home, aurora_home=p.aurora_home, network=p.network, writable=[],
                   hide=[*p.hide, hidden_dir])
        a = argv(q, "sh -c " + shlex.quote(_PROBE) + " probe " + shlex.quote(probe_ws) + " " + shlex.quote(hidden_dir),
                 probe_ws, "/bin/sh")
        r = subprocess.run(a, capture_output=True, text=True, timeout=20, env=env(dict(os.environ)),
                           stdin=subprocess.DEVNULL)
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0 and out.endswith("ok"):
            return True, ""
        return False, f"{tool} trial run failed: {out[-200:] or 'exit ' + str(r.returncode)}"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{tool} trial run failed: {e}"
    finally:
        shutil.rmtree(probe_ws, ignore_errors=True)
        shutil.rmtree(hidden_dir, ignore_errors=True)


def policy_from(runtime: dict, aurora_home: str) -> Policy:
    def strs(v):
        return [str(x) for x in v] if isinstance(v, list) else []
    return Policy(home=os.path.expanduser("~"), aurora_home=str(aurora_home),
                  network=runtime.get("sandbox_network", True) is not False,
                  writable=strs(runtime.get("sandbox_writable")), hide=strs(runtime.get("sandbox_hide")))


def mode_from(runtime: dict) -> str:
    m = str(runtime.get("sandbox", "web")).strip().lower()
    return m if m in MODES else "web"
