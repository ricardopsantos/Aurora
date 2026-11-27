"""Bash mode (`!` / `$`, R10): the user's private side terminal.

R452. Everything here is about commands the USER types; the model's
`run_command` tool shares only the low-level runner (`tools._run_command_once`).
Bash-mode output is never added to the model's context: that is the point of
the mode.

- Each command runs in a fresh bash, but its final directory and environment
  are read back and reused by the next one, so `cd foo && make`, `cd -`,
  `export X=1` and `source .venv/bin/activate` carry over (aliases and shell
  functions do not).
- Colours are forced on when the command's output goes straight to the
  screen (no pipe or redirect), since the TUI renders ANSI colour.
- Full-screen and interactive programs are refused with a hint instead of
  running against /dev/null and failing silently.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass, field

from . import tools

# Typed commands may run long (a build, a test suite). Esc-Esc cancels them
# (R452), so the 300 s tool timeout is not needed as the only way out; one
# hour still bounds a forgotten `tail -f`.
BASH_MODE_TIMEOUT = 3600

# Variables the shell itself changes on every run: carrying them over would
# make SHLVL climb by one per command.
_NOT_CARRIED = {"SHLVL", "_"}

# A pipe, redirect or command substitution: the output is not going straight
# to the screen, so forced colour would end up in a file or another program.
_NOT_TO_SCREEN = re.compile(r"[|>]|\$\(|`")

_EDITORS = {"vi", "vim", "nvim", "nano", "emacs", "pico", "micro", "joe", "mcedit"}
_FULLSCREEN = {"top", "htop", "btop", "atop", "tmux", "screen", "mc", "ranger",
               "nnn", "watch", "nmtui", "alsamixer", "ncdu"}
_REPLS = {"python", "python3", "ipython", "node", "irb", "psql", "mysql",
          "sqlite3", "bash", "sh", "zsh", "fish", "lua", "R", "ghci", "redis-cli"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class ShellState:
    """Directory and environment carried from one bash-mode command to the next."""
    cwd: str
    env: dict = field(default_factory=dict)


@dataclass
class Result:
    out: str                  # raw combined output
    code: int | None          # exit code; None on timeout
    cancelled: bool = False


def initial_state(cwd: str) -> ShellState:
    """The starting environment is the one tools get (secrets removed, R448)."""
    env = tools._tool_env()
    env["PWD"] = cwd
    return ShellState(cwd=cwd, env=env)


def _words(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def needs_terminal(cmd: str) -> str | None:
    """A one-line hint when `cmd` needs a real terminal (a full-screen
    program, an editor, a REPL, a login), else None. stdin is /dev/null
    (R271) and output goes to a pipe, so such a command would fail or exit
    at once without saying why."""
    words = _words(cmd)
    while words and _ENV_ASSIGN.match(words[0]):
        words = words[1:]
    if words and words[0] in ("sudo", "doas", "env", "nice", "time", "command"):
        words = [w for w in words[1:] if not w.startswith("-") and not _ENV_ASSIGN.match(w)]
    if not words:
        return None
    prog, args = os.path.basename(words[0]), words[1:]
    why = None
    if prog in _EDITORS and "--batch" not in args and "-es" not in args:
        why = "is an editor"
    elif prog in _FULLSCREEN:
        why = "is a full-screen program"
    elif prog in _REPLS and not args:
        why = "with no arguments is an interactive shell"
    elif prog in ("ssh", "mosh"):
        hosts = [a for a in args if not a.startswith("-")]
        if len(hosts) <= 1 and "-V" not in args:
            why = "without a remote command opens an interactive login"
    elif prog == "git" and args[:1] == ["commit"] and not any(
            _gives_message(a) for a in args[1:]):
        why = "without -m opens an editor"
    if why is None:
        return None
    return (f"`{words[0]}` {why}: it needs a terminal, which bash mode does not "
            "have. Run it outside Aurora (or in another terminal).")


def _gives_message(arg: str) -> bool:
    """Does this `git commit` argument supply the message (so no editor)?"""
    if arg.startswith("--"):
        return arg.split("=")[0] in ("--message", "--file", "--no-edit",
                                     "--fixup", "--reuse-message")
    # a short-flag cluster: -m, -am, -mWIP, -F file, -C <commit>. A flag
    # that takes a value ends the cluster, so only the letters before the
    # first m/F/C matter, and any of those three means a message is given.
    if not arg.startswith("-") or len(arg) < 2:
        return False
    return any(c in "mFC" for c in arg[1:])


def failure_hint(cmd: str, code: int | None, out: str) -> str | None:
    """A hint for a command that failed because nobody can type (sudo's
    password prompt)."""
    if code in (0, None):
        return None
    low = out.lower()
    if "sudo" in _words(cmd)[:3] and ("a terminal is required" in low
                                       or "password is required" in low
                                       or "no tty present" in low):
        return ("sudo needs a password and bash mode cannot ask for one: run "
                "`sudo -v` in another terminal first, or run it outside Aurora.")
    return None


def color_env(cmd: str, env: dict) -> dict:
    """`env` plus the variables that make common tools colour their output,
    only when the output goes straight to the screen."""
    if _NOT_TO_SCREEN.search(cmd):
        return env
    env = dict(env)
    env.update({"CLICOLOR": "1", "CLICOLOR_FORCE": "1", "FORCE_COLOR": "1",
                "PY_COLORS": "1"})
    if "GIT_CONFIG_COUNT" not in env:
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "color.ui",
                    "GIT_CONFIG_VALUE_0": "always"})
    return env


def _color_prelude(cmd: str) -> str:
    """GNU ls/grep ignore CLICOLOR_FORCE; aliases make them colour too.
    Only for a command whose output goes straight to the screen, and only on
    Linux (GNU tools)."""
    if _NOT_TO_SCREEN.search(cmd) or not sys.platform.startswith("linux"):
        return ""
    if not (tools._shell_executable() or "").endswith("bash"):
        return ""
    return ("shopt -s expand_aliases\n"
            "alias ls='ls --color=always' grep='grep --color=always' "
            "diff='diff --color=always'\n")


_SAVE_STATE_PY = (
    "import os,sys,json\n"
    "c=os.getcwd();p=os.environ.get('PWD')\n"
    "c=p if p and os.path.isdir(p) and os.path.realpath(p)==os.path.realpath(c) else c\n"
    "json.dump({'cwd':c,'env':dict(os.environ)},open(sys.argv[1],'w'))\n")


def wrap(cmd: str, state_file: str) -> str:
    """The script actually run: `cmd`, with an EXIT trap that writes the
    shell's final directory and environment to `state_file`. The trap keeps
    the command's own exit code. A command that sets its own EXIT trap
    replaces this one; nothing is carried over then."""
    save = (f"{shlex.quote(sys.executable)} -I -S -c {shlex.quote(_SAVE_STATE_PY)} "
            f"{shlex.quote(state_file)} 2>/dev/null")
    return ("__aurora_save() { __aurora_rc=$?; " + save + "; exit $__aurora_rc; }\n"
            "trap __aurora_save EXIT\n" + _color_prelude(cmd) + cmd + "\n")


def _read_state(path: str, old: ShellState) -> ShellState:
    try:
        with open(path) as f:
            data = json.load(f)
        cwd, env = data["cwd"], data["env"]
    except (OSError, ValueError, KeyError, TypeError):
        return old                    # killed, or its own EXIT trap: keep the old state
    if not isinstance(env, dict) or not os.path.isdir(cwd):
        return old
    env = {k: v for k, v in env.items() if k not in _NOT_CARRIED}
    for k in ("CLICOLOR", "CLICOLOR_FORCE", "FORCE_COLOR", "PY_COLORS",
              "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        if k not in old.env:
            env.pop(k, None)          # added for this run only (color_env)
    return ShellState(cwd=cwd, env=env)


def run(cmd: str, state: ShellState, *, cancel=None, on_chunk=None,
        timeout: float = BASH_MODE_TIMEOUT, carry: bool = True
        ) -> tuple[Result, ShellState]:
    """Run one typed command. Returns the result and the state for the next
    command (`state` itself when `carry` is False, for the side channel,
    which must not change it)."""
    # R464: the state file lives in a private (0700) directory. It used to be a mkstemp name in the shared
    # temp dir, unlinked so the trap could recreate it: the name is in the `bash -c` script, which any local
    # user sees in `ps`, so another user could plant a file there first and the next command would run with
    # the environment it names (LD_PRELOAD, PATH). Nobody else can create or replace a file in this directory.
    tmpdir = tempfile.mkdtemp(prefix="aurora-bash-")
    path = os.path.join(tmpdir, "state.json")   # the trap creates it; absent = not written
    try:
        script = wrap(cmd, path) if carry else _color_prelude(cmd) + cmd
        try:
            out, code = tools._run_command_once(
                script, state.cwd, timeout=timeout, cancel=cancel,
                on_chunk=on_chunk, env=color_env(cmd, state.env),
                sandbox=False)                 # R475: the user's own terminal is never boxed
        except tools.CommandCancelled as e:
            return Result(str(e), None, cancelled=True), state
        new = _read_state(path, state) if carry else state
        return Result(out, code), new
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def format_result(res: Result, timeout: float = BASH_MODE_TIMEOUT) -> str:
    """The text shown for a finished command: its output, then a status line
    when it did not succeed. Empty when it succeeded silently."""
    out = res.out.rstrip("\n")
    if res.cancelled:
        tail = "[cancelled]"
    elif res.code is None:
        tail = f"[timeout after {int(timeout)}s]"
    elif res.code != 0:
        tail = f"[exit {res.code}]"
    else:
        return out
    return (out + "\n" if out.strip() else "") + tail


_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")   # R548: + OSC (ls/git hyperlinks)


def plain_text(text: str) -> str:
    """`text` without colour codes: what "copy last" puts on the clipboard
    (the colours forced on by `color_env` are for the screen only)."""
    return _CSI.sub("", text)


def cd_into(state: ShellState, target: str) -> tuple[ShellState, str | None]:
    """A plain `cd <path>` handled without a shell (it keeps accepting an
    unquoted path with spaces, as Tab completion inserts it). Returns the new
    state and an error, or None."""
    new_dir = os.path.normpath(os.path.join(state.cwd, os.path.expanduser(target)))
    if not os.path.isdir(new_dir):
        return state, f"cd: no such file or directory: {target}"
    if not os.access(new_dir, os.X_OK):           # R549: it used to "succeed" and every next command failed
        return state, f"cd: permission denied: {target}"
    env = dict(state.env)
    env["OLDPWD"], env["PWD"] = state.cwd, new_dir
    return ShellState(cwd=new_dir, env=env), None


def plain_cd_target(target: str | None) -> str | None:
    """`target` when Python can resolve it itself, None when bash must
    (`cd -`, `cd $VAR`, `cd ~user`, globs)."""
    if target is None:
        return None
    if target == "-" or any(c in target for c in "$`*?[\"'\\") or (     # R550: quotes, escapes: bash
            target.startswith("~") and not target.startswith(("~/",)) and target != "~"):
        return None
    return target
