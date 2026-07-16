"""Approval gate for mutating tools (R7) with a persistent pattern allowlist
(R7) and a diff preview for writes/edits (R8).

Allowlist file: AURORA_HOME/allowlist.yaml
  run_command:   list of command *prefixes* auto-approved
  write_file:    list of path globs auto-approved
  edit_file:     list of path globs auto-approved
"""

import difflib
import fnmatch
import os
import shlex
from pathlib import Path

import yaml

from .paths import aurora_home


def _norm_command(cmd: str) -> list[str]:
    """Tokenize a shell command for allowlist matching so equivalent spellings
    collapse to the same tokens: quotes are stripped and ~ is expanded. Thus
    `bash ~/x.sh`, `bash "/home/me/x.sh"` and `bash /home/me/x.sh` all match a
    single stored rule — otherwise an 'always allow' never catches the model's
    next run of the same command in a different spelling."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()           # unbalanced quotes etc. — best effort
    return [os.path.expanduser(t) for t in toks]

# Read-only commands whose only real variation between invocations is the
# path/pattern argument — "always allow" on one path shouldn't force a
# re-approval for the same command against a different path next session.
# Deliberately narrow: nothing here writes, deletes, or executes arbitrary
# code, so generalizing the rule to "any args" carries no extra risk.
SAFE_COMMANDS = frozenset({
    "find", "ls", "tree", "grep", "cat", "pwd", "whoami", "which", "wc",
    "head", "tail", "file",
})

_FILE = "allowlist.yaml"


def _path() -> Path:
    return aurora_home() / _FILE


def load() -> dict:
    p = _path()
    if not p.exists():
        return {"run_command": [], "write_file": [], "edit_file": []}
    data = yaml.safe_load(p.read_text()) or {}
    for k in ("run_command", "write_file", "edit_file"):
        data.setdefault(k, [])
    return data


def save(data: dict) -> None:
    _path().write_text(yaml.safe_dump(data, sort_keys=False))


def _signature(tool: str, args: dict) -> str:
    """The value matched against the allowlist: command prefix or file path."""
    if tool == "run_command":
        return args.get("command", "")
    return args.get("path", "")


def is_allowed(tool: str, args: dict, data: dict | None = None) -> bool:
    data = data or load()
    sig = _signature(tool, args)
    if tool == "run_command":
        # token-boundary prefix match, on NORMALIZED tokens (quotes stripped,
        # ~ expanded) so path-spelling variants of the same command match: an
        # allowlisted "git status" approves "git status --short" but never
        # "gitk". Legacy single-token rules ("rm") only match the bare command
        # EXACTLY — "rm" must not auto-approve "rm -rf /".
        sig_toks = _norm_command(sig)
        for p in data["run_command"]:
            if not p:
                continue
            rule = _norm_command(p)
            if not rule:
                continue
            if sig_toks == rule or (len(rule) >= 2
                                    and sig_toks[:len(rule)] == rule):
                return True
            # single-token rule for a known-safe read-only command: prefix
            # match regardless of args (see SAFE_COMMANDS above). A
            # single-token rule for anything else stays exact-match-only —
            # "rm" must never auto-approve "rm -rf /".
            if (len(rule) == 1 and rule[0] in SAFE_COMMANDS
                    and sig_toks[:1] == rule):
                return True
        return False
    return any(fnmatch.fnmatch(sig, g) for g in data.get(tool, []) if g)


def legacy_rules(data: dict | None = None) -> list[str]:
    """Single-token run_command rules from before the two-token fix — still
    honored, but demoted to exact-match only (unless the command is in
    SAFE_COMMANDS, where a single token is intentional, not legacy — see
    add_rule). Surfaced so the user prunes the genuinely stale ones."""
    data = data or load()
    return [p for p in data.get("run_command", [])
            if p and len(p.split()) < 2 and p not in SAFE_COMMANDS]


def add_rule(tool: str, args: dict) -> str:
    """Persist an 'always' answer. For commands, store the first TWO tokens
    ("rm -rf", "git push") — a bare first token ("rm") auto-approves far more
    than the human just looked at. For files, the exact path. Returns the
    stored rule for display."""
    data = load()
    if tool == "run_command":
        # store the first two NORMALIZED tokens (quotes stripped, ~ expanded)
        # with shlex.join so a token containing spaces round-trips losslessly
        toks = _norm_command(args.get("command", ""))
        # a known-safe read-only command generalizes across ANY args (the
        # varying part is always just a path/pattern) — store the bare
        # command name so "always allow" on `find /a` also covers `find /b`
        # in a different project/session instead of re-prompting per path
        rule = toks[0] if toks and toks[0] in SAFE_COMMANDS \
            else shlex.join(toks[:2])
    else:
        rule = args.get("path", "")
    if rule and rule not in data[tool]:
        data[tool].append(rule)
        save(data)
    return rule


def diff_preview(tool: str, args: dict) -> str:
    """A short unified diff for write/edit; empty for run_command. Must never
    raise: it runs inside the agent loop AFTER the assistant message (with
    its tool_use) is already in history — an exception here (binary target,
    permission error) would kill the turn and leave that tool_use dangling,
    poisoning every later request."""
    try:
        return _diff_preview(tool, args)
    except Exception as e:
        return f"[diff unavailable: {e.__class__.__name__}: {e}]"


def _diff_preview(tool: str, args: dict) -> str:
    if tool == "write_file":
        path = Path(args["path"]).expanduser()
        old = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        new = args.get("content", "").splitlines()
        d = difflib.unified_diff(old, new, "before", "after", lineterm="")
    elif tool == "edit_file":
        path = Path(args["path"]).expanduser()
        if not path.is_file():
            return "[new edit — file does not exist]"
        text = path.read_text(encoding="utf-8")
        d = difflib.unified_diff(
            text.splitlines(),
            text.replace(args.get("old", ""), args.get("new", ""), 1).splitlines(),
            "before", "after", lineterm="")
    else:
        return ""
    lines = list(d)
    if len(lines) > 60:
        lines = lines[:60] + [f"... (+{len(lines) - 60} more diff lines)"]
    return "\n".join(lines)
