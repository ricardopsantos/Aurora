"""Approval gate for mutating tools (R7) with a persistent pattern allowlist
(R7) and a diff preview for writes/edits (R8).

Allowlist file: AURORA_HOME/allowlist.yaml
  run_command:   list of command *prefixes* auto-approved
  write_file:    list of path globs auto-approved
  edit_file:     list of path globs auto-approved
  apply_patch:   list of path globs auto-approved (R97)
  wait_until:    list of command *prefixes* auto-approved (R100) — its own
                 bucket, separate from run_command's
"""

import difflib
import fnmatch
import functools
import os
import shlex
from pathlib import Path

import yaml

from .paths import aurora_home


@functools.lru_cache(maxsize=512)
def _norm_command(cmd: str) -> tuple[str, ...]:
    """Tokenize a shell command for allowlist matching so equivalent spellings
    collapse to the same tokens: quotes are stripped and ~ is expanded. Thus
    `bash ~/x.sh`, `bash "/home/me/x.sh"` and `bash /home/me/x.sh` all match a
    single stored rule — otherwise an 'always allow' never catches the model's
    next run of the same command in a different spelling.

    R96h: `is_allowed` calls this once per RULE per check — the same
    allowlist entries get re-`shlex.split()`'d on every single tool call in
    a turn, even though the rule strings themselves never change between
    calls (`load()` only changes when the user adds a rule). `lru_cache`
    turns that into "tokenize each distinct command string once, ever" —
    512 is comfortably above any real allowlist's rule count plus a
    session's distinct commands; a cache miss just re-tokenizes, so an
    eviction costs nothing but the one-time cost this fix removes.
    Returns a tuple, not a list, so the result is hashable and safe to share
    across callers (a shared mutable list would let one caller's mutation
    corrupt the cache for every other caller)."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()           # unbalanced quotes etc. — best effort
    return tuple(os.path.expanduser(t) for t in toks)

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


_TOOLS = ("run_command", "write_file", "edit_file", "apply_patch", "wait_until")


def load() -> dict:
    p = _path()
    if not p.exists():
        return {k: [] for k in _TOOLS}
    data = yaml.safe_load(p.read_text()) or {}
    for k in _TOOLS:
        data.setdefault(k, [])
    return data


def save(data: dict) -> None:
    _path().write_text(yaml.safe_dump(data, sort_keys=False))


def _norm_path(path: str) -> str:
    """R95g: the path a file rule is stored and matched as. `run_command`
    rules already normalize their tokens so spelling variants of the same
    command match one rule; file rules did raw `fnmatch` on whatever the
    model passed, so `~/x.py` and `/home/me/x.py` were two different rules
    and "always allow" re-prompted on the other spelling. Expanded but NOT
    resolved — resolving would follow symlinks and collapse the `*` in a
    glob rule, and a rule is allowed to be a glob."""
    return os.path.expanduser(path) if path else path


# R100: wait_until is a repeated shell command, same shape as run_command,
# so it gets the same command-prefix matching — but its OWN allowlist
# bucket. An "always allow" made for one must never silently cover the
# other: a plain one-shot command and "keep re-running this until it
# succeeds" are different enough risk shapes that conflating their rules
# would surprise someone who only meant to approve one of them.
_COMMAND_TOOLS = ("run_command", "wait_until")


def _signature(tool: str, args: dict) -> str:
    """The value matched against the allowlist: command prefix or file path."""
    if tool in _COMMAND_TOOLS:
        return args.get("command", "")
    return _norm_path(args.get("path", ""))


def is_allowed(tool: str, args: dict, data: dict | None = None) -> bool:
    data = data or load()
    sig = _signature(tool, args)
    if tool in _COMMAND_TOOLS:
        # token-boundary prefix match, on NORMALIZED tokens (quotes stripped,
        # ~ expanded) so path-spelling variants of the same command match: an
        # allowlisted "git status" approves "git status --short" but never
        # "gitk". Legacy single-token rules ("rm") only match the bare command
        # EXACTLY — "rm" must not auto-approve "rm -rf /".
        sig_toks = _norm_command(sig)
        for p in data[tool]:
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
    # rules are normalized on both sides, so a rule stored before R95g (raw
    # `~/x.py`) still matches a normalized signature
    return any(fnmatch.fnmatch(sig, _norm_path(g))
               for g in data.get(tool, []) if g)


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
    if tool in _COMMAND_TOOLS:
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
        rule = _norm_path(args.get("path", ""))
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
        # R95a: honour replace_all. The preview must show what the edit will
        # ACTUALLY do — a fixed count of 1 here previewed one changed line
        # while `tools.edit_file(replace_all=True)` then changed every
        # occurrence, so the human approved a diff that wasn't the change.
        old, new = args.get("old", ""), args.get("new", "")
        count = -1 if args.get("replace_all") else 1
        d = difflib.unified_diff(
            text.splitlines(),
            text.replace(old, new, count).splitlines(),
            "before", "after", lineterm="")
    elif tool == "apply_patch":
        path = Path(args["path"]).expanduser()
        if not path.is_file():
            return "[error: no such file — apply_patch requires an existing file]"
        # R97, same principle as R95a: show what the patch will ACTUALLY do,
        # by really parsing+applying it here — never the model's raw
        # submitted diff text verbatim, which may not match what apply()
        # will really produce (or may fail outright; that failure surfaces
        # to the human as the preview itself, via diff_preview()'s outer
        # exception guard, rather than only being discovered after approval)
        from . import patch as patchmod
        text = path.read_text(encoding="utf-8")
        hunks = patchmod.parse(args.get("diff", ""))
        new_text = patchmod.apply(text, hunks)
        d = difflib.unified_diff(text.splitlines(), new_text.splitlines(),
                                 "before", "after", lineterm="")
    else:
        return ""
    lines = list(d)
    if len(lines) > 60:
        lines = lines[:60] + [f"... (+{len(lines) - 60} more diff lines)"]
    return "\n".join(lines)
