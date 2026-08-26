"""Approval gate for mutating tools (R7) with a persistent pattern allowlist
(R7) and a diff preview for writes/edits (R8).

Allowlist file: AURORA_HOME/allowlist.yaml
  run_command:   list of command *prefixes* auto-approved
  write_file:    list of path globs auto-approved
  edit_file:     list of path globs auto-approved
  apply_patch:   list of path globs auto-approved (R97)
  wait_until:    list of command *prefixes* auto-approved (R100) — its own
                 bucket, separate from run_command's
  mcp_<server>_<tool>: same shape (path/command matching, whichever
                 applies) — extension tools use their own full name as the
                 bucket key, auto-created on first "always allow" (R119/R120)

Denylist file: AURORA_HOME/denylist.yaml (R120) — same shape and matching
rules as the allowlist, but for "always DENY this" instead: a match skips
the approval prompt entirely and auto-refuses, no question asked. Checked
BEFORE the allowlist on every gated call — deny always wins, matching
pi-permission-system's fail-closed design (a case both an allow and a deny
rule ever match is a config mistake, not a case worth defining new
precedence rules for; deny winning is the conservative choice either way).
"""

import difflib
import fnmatch
import functools
import os
import re
import shlex
from pathlib import Path

import yaml

from . import rewind
from .paths import aurora_home, write_text_atomic


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

# R149: the exact MIRROR of SAFE_COMMANDS. Where a safe command generalizes
# across any args, these generalize across none — an "always allow" on one
# covers that whole command string and nothing else.
#
# The bug this closes is not a missing list entry, it's the direction the
# two-token rule generalizes in. `_rule_for` stores the first two tokens,
# which for a destructive command is precisely the HARMLESS half, leaving the
# target free to vary:
#
#   "always allow" on `rm -rf ./build`          stores `rm -rf`
#       → thereafter auto-approves `rm -rf /` and `rm -rf ~`, unprompted
#   "always allow" on `dd if=/dev/zero of=./f`  stores `dd if=/dev/zero`
#       → thereafter auto-approves `of=/dev/disk0`
#
# The old code comment claimed two-token storage was "correct for anything
# that can write/delete/execute, since a bare `rm` must never auto-approve
# `rm -rf /`" — but that guard only ever covered the SINGLE-token legacy
# case. The two-token case walked straight into it.
DANGEROUS_COMMANDS = frozenset({
    # disk/device writers — the `dd`-as-disk-destroyer family
    "dd", "mkfs", "fdisk", "parted", "shred", "hdparm", "diskutil", "newfs",
    # deleters
    "rm", "rmdir", "unlink", "srm",
    # interpreters: the ARGS are the program, so no prefix of them is safe
    "sh", "bash", "zsh", "dash", "ksh", "fish", "eval", "source", "exec",
    "python", "python2", "python3", "perl", "ruby", "node", "osascript",
    # privilege escalation and permission changes. `sudo` matters most: it
    # makes the REST of the line dangerous whatever that line is
    "sudo", "su", "doas", "chown", "chmod", "chgrp", "launchctl", "systemctl",
    # fetch-to-shell — the classic `curl … | sh`, and a plain download that
    # overwrites a file is a write either way
    "curl", "wget",
    # R190: plain writers/destroyers. The two-token rule stores the command
    # plus its FIRST argument, which for all of these is the SOURCE — leaving
    # the destination (the thing actually overwritten) free to vary:
    #   "always allow" on `mv ./notes.md ./archive/`  stores `mv ./notes.md`
    #       → thereafter auto-approves `mv ./notes.md ~/.bashrc`
    # Same shape as the `rm -rf` / `dd if=…` cases R149 closed; these are the
    # entries that list was missing rather than a new class of problem.
    "mv", "cp", "ln", "install", "truncate", "tee",
    # container runtimes: `docker run -v /:/mnt` is unrestricted host access,
    # and `docker run <img>` two-token-stored leaves every flag free to vary
    "docker", "podman",
    # package managers execute arbitrary code from the fetched package
    # (setup.py, npm install scripts, Makefiles), so the package NAME is the
    # payload and must never be the free-to-vary half of a two-token rule
    "pip", "pip3", "npm", "npx", "yarn", "pnpm", "gem", "cargo", "make",
    "apt", "apt-get", "brew",
})

# families whose real binary name carries a suffix: mkfs.ext4, newfs_hfs
_DANGEROUS_PREFIXES = ("mkfs.", "newfs_")

# R242: a VERSIONED spelling of a dangerous command is the same command.
# `DANGEROUS_COMMANDS` is matched by exact basename, so `python3` was caught
# and `python3.11` — the standard binary name on most Linux distros and
# Homebrew — was not. The consequence is R149's bug reopened on the entry
# R149 calls out as worst ("interpreters: the ARGS are the program, so no
# prefix of them is safe"): approving a harmless `python3.11 -c "print(1)"`
# stores the two-token rule `python3.11 -c`, which thereafter auto-approves
# `python3.11 -c "<anything>"` with no prompt, forever. Verified end to end
# before the fix.
#
# Stripping a trailing version suffix (`python3.11`→`python`, `pip3`→`pip`,
# `perl5.36`→`perl`, `node20`→`node`) and re-testing membership covers the
# whole family without enumerating releases. Over-triggering is harmless
# here — it only means a command matches exact-only and the user is asked
# again — while under-triggering is what just cost a silent approval.
_VERSION_SUFFIX = re.compile(r"[-_]?[0-9][0-9._-]*$")


def _unversioned(base: str) -> str:
    return _VERSION_SUFFIX.sub("", base)

# R190: a command that is read-only in its ordinary use but carries a
# write/delete/exec primitive behind a FLAG. Membership in SAFE_COMMANDS is a
# claim about the command NAME ("nothing here writes, deletes, or executes"),
# and for `find` that claim is only true of how it is usually invoked — the
# binary itself ships `-delete`, `-fprintf`, `-fls` and `-exec`.
#
# That matters because SAFE_COMMANDS generalizes in the widest possible way:
# `_rule_for` stores the BARE name, and `_matches` lets a single-token rule
# prefix-match any args. So one "always allow" on a routine `find . -name
# '*.log'` stored the rule `find` — and from then on `find / -delete` was
# auto-approved, unprompted, forever.
#
# This is R149's bug arriving through the opposite door. R149 stopped a
# command KNOWN to be destructive from generalizing over its target; this
# stops a command MIS-CLASSIFIED as safe from generalizing at all. The fix
# keeps the useful behaviour (a plain `find` still generalizes across paths,
# which is the whole point of the SAFE_COMMANDS list) and removes only the
# invocations that can actually mutate something.
#
# `-exec`/`-execdir` are listed even though `_is_dangerous` already catches
# `find . -exec rm {} +` via the `rm` token: it only catches it when the
# EXECUTED program is itself a known-dangerous name, so `-exec truncate`,
# `-exec tee` or `-exec ./script.sh` slipped through.
_UNSAFE_FLAGS = {
    "find": frozenset({
        "-delete", "-exec", "-execdir", "-ok", "-okdir",
        "-fprint", "-fprint0", "-fprintf", "-fls",
    }),
}

# R141: shell syntax that turns one command string into several, or into a
# write. Checked against the RAW command, never its tokens: `run_command`
# executes with `shell=True`, but `shlex.split` — what the allowlist matches
# on — treats `&&`, `||`, `|`, `>` as ordinary WORDS and a newline as plain
# whitespace. So `ls && rm -rf ~` tokenizes to ("ls", "&&", "rm", …) and
# prefix-matched a stored `ls` rule: every token-boundary guarantee the
# matcher advertises silently stopped at the first operator.
#
# `$` alone is deliberately absent — `echo $HOME` is expansion, not
# execution, and is far too common to force a re-prompt on; `$(` is the
# substitution form and IS listed. Quoted-but-harmless uses (`grep 'a|b' f`)
# do get caught by the raw-string check and will re-prompt. That is the
# intended trade: on a security gate, a false prompt costs a keystroke and a
# false auto-approval costs the user's filesystem.
_SHELL_OPS = ("&", "|", ";", "<", ">", "`", "$(", "\n", "\r")


def _is_dangerous(toks: tuple) -> bool:
    """Does any token name a command that must never match by prefix (R149)?

    EVERY token is checked, not just the first, and by BASENAME. `sudo dd …`,
    `/bin/rm -rf /`, `xargs rm -rf`, `env dd …`, `nice rm …` and
    `find . -exec rm {} +` all put the dangerous name somewhere other than
    position zero, and a first-token-only test would generalize the two-token
    rule right over the top of them (`xargs rm` covering `xargs rm -rf /`).

    Scanning every token does over-trigger — `grep dd .` has a bare `dd`
    token and gets treated as dangerous. That costs one re-approval when the
    args change and nothing else, which is the right side to err on. Note it
    only fires on a token that IS the name: `git commit -m "remove dd stuff"`
    is a single token and does not match."""
    names = set()
    for t in toks:
        base = os.path.basename(t)
        # R242: `python3.11` is `python`. Both spellings are tested so an
        # exact entry still wins without depending on the strip.
        if (base in DANGEROUS_COMMANDS
                or _unversioned(base) in DANGEROUS_COMMANDS
                or base.startswith(_DANGEROUS_PREFIXES)):
            return True
        names.add(base)
    # R190: an otherwise-safe command invoked with one of its own mutating
    # flags (`find … -delete`). Both halves must be present, so a bare
    # `-delete` token belonging to some other command doesn't trip this.
    for cmd, flags in _UNSAFE_FLAGS.items():
        if cmd in names and not flags.isdisjoint(toks):
            return True
    return False


def _is_compound(cmd: str) -> bool:
    """Does this command string contain shell syntax that could chain,
    redirect, or substitute another command into it?"""
    return any(op in cmd for op in _SHELL_OPS)

_FILE = "allowlist.yaml"
_DENY_FILE = "denylist.yaml"


def _path() -> Path:
    return aurora_home() / _FILE


def _deny_path() -> Path:
    return aurora_home() / _DENY_FILE


_TOOLS = ("run_command", "write_file", "edit_file", "apply_patch", "wait_until")


class ApproveLoadError(Exception):
    """R170a: a present-but-unparseable allowlist/denylist file used to
    collapse to `{}` via `yaml.safe_load(...) or {}` (or crash on a non-dict
    result with an unrelated AttributeError downstream), which made
    `is_denied` silently return `False` — a corrupt denylist.yaml disabled
    ALL deny enforcement with no visible error. Raised instead so callers can
    fail CLOSED (block gated calls) rather than fail open, matching R120's
    'deny always wins' guarantee. A genuinely missing or truly empty file is
    NOT an error — that's the documented "no rules yet" case."""


def _load(path: Path, prepopulate: bool) -> dict:
    """Shared by load()/load_deny(). `prepopulate`: the allowlist always
    pre-creates the 5 known gated tools' buckets (existing behavior,
    depended on by callers that assume the key exists); the denylist
    doesn't need to, since a fresh install has no deny rules for anything.
    Either way, an unknown tool name (an extension's, e.g. `mcp_github_x`)
    still round-trips fine — `_add_rule`/`is_allowed`/`is_denied` all use
    `.get`/`setdefault` rather than assuming the key pre-exists (R120 bug
    fix: `add_rule` used to KeyError on any tool name outside the fixed
    `_TOOLS` tuple, so "always allow" on an extension tool crashed the
    turn instead of persisting)."""
    if not path.exists():
        return {k: [] for k in _TOOLS} if prepopulate else {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ApproveLoadError(f"{path}: {e}") from e
    if not isinstance(data, dict):
        raise ApproveLoadError(
            f"{path}: expected a mapping, got {type(data).__name__}")
    if prepopulate:
        for k in _TOOLS:
            data.setdefault(k, [])
    return data


def load() -> dict:
    return _load(_path(), prepopulate=True)


def load_deny() -> dict:
    return _load(_deny_path(), prepopulate=False)


def save(data: dict) -> None:
    write_text_atomic(_path(), yaml.safe_dump(data, sort_keys=False))


def save_deny(data: dict) -> None:
    write_text_atomic(_deny_path(), yaml.safe_dump(data, sort_keys=False))


def _norm_path(path: str) -> str:
    """R95g: the path a file rule is stored and matched as. `run_command`
    rules already normalize their tokens so spelling variants of the same
    command match one rule; file rules did raw `fnmatch` on whatever the
    model passed, so `~/x.py` and `/home/me/x.py` were two different rules
    and "always allow" re-prompted on the other spelling. Expanded but NOT
    resolved — resolving would follow symlinks and collapse the `*` in a
    glob rule, and a rule is allowed to be a glob.

    R195: also `normpath`, which is what closes a real escape. `fnmatch`'s
    `*` crosses `/` (it is not glob), so a stored rule of `~/project/*`
    matched the signature `~/project/../../etc/passwd` — the traversal
    segments were just more characters for `*` to swallow. A user who
    approved "always allow writes under my project" was silently
    auto-approving writes ANYWHERE on the filesystem, and `tools._resolve`
    only expands `~`, so the write really did land outside. Verified
    end-to-end before the fix: a rule for `/tmp/x/project/*` auto-approved
    writing `/tmp/x/secret/keys.txt`.

    `normpath` is purely LEXICAL — it collapses `..` without touching the
    disk, so the "expanded but not resolved" property above is preserved and
    a glob rule stays a glob. Applied to both sides (rule and signature),
    since `_matches` runs every rule through here too.

    R195 left a residual — a SYMLINK inside the approved directory pointing
    outside it still matched — on the grounds that catching it needs a real
    `resolve()` and a glob rule has no filesystem identity to resolve. R209
    closes it; see `_resolved_rule`."""
    return os.path.normpath(os.path.expanduser(path)) if path else path


# R209: the characters that make a rule a GLOB rather than a literal path.
_GLOB_CHARS = "*?["


def _resolved_sig(path: str) -> str:
    """The signature with symlinks followed. Non-strict, so a file that does
    not exist yet — `write_file` creating one — still resolves through its
    real parent instead of raising."""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return _norm_path(path)


def _resolved_rule(rule: str) -> str:
    """R209: a rule with its LITERAL prefix resolved and its glob tail left
    alone — `~/proj/*` becomes `/real/path/to/proj/*`.

    This is what makes the symlink half checkable. R195 closed `..` purely
    lexically and recorded the symlink case as open, because you cannot
    `resolve()` a pattern. But you can resolve the part of it that is a real
    path: split at the first glob character, back up to the last whole path
    segment (so `/a/b*` resolves `/a`, never `/a/b`), resolve that, and
    re-attach the rest verbatim.

    Resolving BOTH sides is what keeps this from over-prompting. Matching a
    resolved signature against an unresolved rule would break the ordinary
    case on macOS, where `/tmp` is itself a symlink to `/private/tmp`: every
    rule under it would stop matching and re-prompt forever."""
    r = _norm_path(rule)
    hits = [r.find(c) for c in _GLOB_CHARS if c in r]
    if not hits:
        head, tail = r, ""
    else:
        cut = r.rfind(os.sep, 0, min(hits))
        if cut == -1:
            return r                      # glob in the first segment
        head, tail = r[:cut], r[cut:]
    try:
        return str(Path(head).resolve()) + tail
    except OSError:
        return r


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


def _matches(tool: str, args: dict, data: dict, strict: bool = False) -> bool:
    """Shared by is_allowed/is_denied — same pattern rules, different file.
    A rule from an allowlist means "always approve"; the identical rule
    shape in a denylist means "always refuse" — the matching logic itself
    doesn't know or care which.

    `strict` (R141, extended by R149) is the one place the two directions
    must NOT agree. For the allowlist it disables prefix generalization on a
    compound command (so a stored `ls` can never approve `ls && rm -rf ~`)
    and on a dangerous one (so a stored `rm -rf` can never approve
    `rm -rf /`). Applying either restriction to the DENYlist would weaken it
    — a denied `rm` must still be denied when chained — so `is_denied` leaves
    it off. Both directions stay fail-closed; they just fail toward different
    answers."""
    sig = _signature(tool, args)
    if tool in _COMMAND_TOOLS:
        # These may only ever match a rule EXACTLY. The user explicitly
        # approved that whole command, which is still a real thing to want,
        # but nothing about it generalizes to a prefix: for a pipeline the
        # tail can be swapped (R141), and for a destructive command the
        # two-token rule holds the harmless half and leaves the TARGET free
        # to vary (R149).
        exact_only = strict and (_is_compound(sig)
                                 or _is_dangerous(_norm_command(sig)))
        # token-boundary prefix match, on NORMALIZED tokens (quotes stripped,
        # ~ expanded) so path-spelling variants of the same command match: an
        # allowlisted "git status" approves "git status --short" but never
        # "gitk". Legacy single-token rules ("rm") only match the bare command
        # EXACTLY — "rm" must not auto-approve "rm -rf /".
        sig_toks = _norm_command(sig)
        for p in data.get(tool, []):
            if not p:
                continue
            rule = _norm_command(p)
            if not rule:
                continue
            if sig_toks == rule:
                return True
            if exact_only:
                continue
            if len(rule) >= 2 and sig_toks[:len(rule)] == rule:
                return True
            # A single-token rule prefix-matches regardless of args — but for
            # the ALLOWlist only when the command is known-safe (see
            # SAFE_COMMANDS): "rm" must never auto-approve "rm -rf /".
            #
            # R149b: on the DENYlist that restriction was backwards. A lone
            # `dd` there matched only the bare word `dd`, so writing the
            # obvious rule to hard-block the disk destroyer blocked nothing
            # real — the exact-match guard exists to stop a vague rule
            # ALLOWING too much, and denying too much is the safe direction.
            # A single-token deny rule now covers any args.
            if (len(rule) == 1 and sig_toks[:1] == rule
                    and (not strict or rule[0] in SAFE_COMMANDS)):
                return True
        return False
    # rules are normalized on both sides, so a rule stored before R95g (raw
    # `~/x.py`) still matches a normalized signature.
    #
    # R209: the ALLOWlist additionally requires the RESOLVED pair to match,
    # which is what closes R195's symlink residual — a link inside an approved
    # directory pointing outside it satisfies the lexical test (the path really
    # is under the rule) and fails the resolved one. The DENYlist keeps
    # matching on either, because there "matches more" is the safe direction
    # and R120's "deny always wins" must not be narrowed by a symlink either.
    rules = [g for g in data.get(tool, []) if g]
    if not strict:
        return any(fnmatch.fnmatch(sig, _norm_path(g))
                   or fnmatch.fnmatch(_resolved_sig(sig), _resolved_rule(g))
                   for g in rules)
    res_sig = _resolved_sig(sig)
    return any(fnmatch.fnmatch(sig, _norm_path(g))
               and fnmatch.fnmatch(res_sig, _resolved_rule(g))
               for g in rules)


def is_allowed(tool: str, args: dict, data: dict | None = None) -> bool:
    return _matches(tool, args, data if data is not None else load(),
                    strict=True)


def is_denied(tool: str, args: dict, data: dict | None = None) -> bool:
    """R120: a matching denylist rule skips the approval prompt entirely and
    auto-refuses — no question asked. Checked before the allowlist by the
    caller (agent.py); an empty/missing denylist.yaml (the common case)
    means nothing is ever denied by policy. R170a: a present-but-corrupt
    denylist.yaml raises `ApproveLoadError` (via `load_deny`) instead of
    silently behaving like an empty one — the caller must fail closed."""
    data = data if data is not None else load_deny()
    if not data:
        return False
    return _matches(tool, args, data)


def legacy_rules(data: dict | None = None) -> list[str]:
    """Single-token run_command rules from before the two-token fix — still
    honored, but demoted to exact-match only (unless the command is in
    SAFE_COMMANDS, where a single token is intentional, not legacy — see
    add_rule). Surfaced so the user prunes the genuinely stale ones."""
    data = data or load()
    return [p for p in data.get("run_command", [])
            if p and len(p.split()) < 2 and p not in SAFE_COMMANDS]


def _rule_for(tool: str, args: dict) -> str:
    """The rule string add_rule/add_deny_rule persist. For commands, the
    first TWO normalized tokens ("rm -rf", "git push") — a bare first token
    ("rm") auto-approves/denies far more than the human just looked at.
    For files, the exact path.

    Bug fix: MCP/extension tools rarely carry a `path` argument at all
    (real args look like `{"title": ..., "body": ...}`), so this used to
    return `""` for them — falsy, so `add_rule`/`add_deny_rule`'s `if rule`
    guard silently skipped saving anything. "Always allow"/"Always DENY"
    picked from the approval prompt then did nothing: the next identical
    call re-prompted (or, for deny, was never actually blocked next time)
    with no visible error. `"*"` is a real, storable rule meaning "this
    tool, any args" — `fnmatch.fnmatch(sig, "*")` matches unconditionally,
    including an empty signature, so `is_allowed`/`is_denied` treat it
    exactly like a path-glob rule already would. This is the accepted,
    documented granularity limit for these tools (tool-level, not
    per-argument) — the bug was that it didn't even work at that
    granularity, not that the granularity itself was coarse."""
    if tool in _COMMAND_TOOLS:
        # store with shlex.join so a token containing spaces round-trips
        # losslessly
        cmd = args.get("command", "")
        toks = _norm_command(cmd)
        # A compound (R141) or dangerous (R149) command is stored WHOLE.
        # Neither generalization below is sound for them: `ls` would approve
        # any chain starting `ls`, the two-token `ls &&` is worse still, and
        # the two-token `rm -rf` leaves the target free to become `/`.
        # Storing the full command keeps "always allow" honest — it matches
        # exactly what the user was shown, and nothing else.
        if _is_compound(cmd) or _is_dangerous(toks):
            return shlex.join(toks)
        # a known-safe read-only command generalizes across ANY args (the
        # varying part is always just a path/pattern) — store the bare
        # command name so "always allow" on `find /a` also covers `find /b`
        # in a different project/session instead of re-prompting per path
        return toks[0] if toks and toks[0] in SAFE_COMMANDS \
            else shlex.join(toks[:2])
    return _norm_path(args.get("path", "")) or "*"


def add_rule(tool: str, args: dict) -> str:
    """Persist an 'always allow' answer. Returns the stored rule for
    display. R120 bug fix: used to do `data[tool].append(...)`, which
    KeyError'd for any tool name outside the fixed `_TOOLS` tuple — an
    extension's tool (e.g. `mcp_github_create_issue`) crashed the turn
    instead of persisting the rule. `setdefault` handles any tool name."""
    data = load()
    rule = _rule_for(tool, args)
    if rule and rule not in data.setdefault(tool, []):
        data[tool].append(rule)
        save(data)
    return rule


def add_deny_rule(tool: str, args: dict) -> str:
    """Persist an 'always DENY' answer (R120) — same rule shape as
    add_rule, opposite file and meaning."""
    data = load_deny()
    rule = _rule_for(tool, args)
    if rule and rule not in data.setdefault(tool, []):
        data[tool].append(rule)
        save_deny(data)
    return rule


def diff_preview(tool: str, args: dict) -> str:
    """A short unified diff for write/edit; empty for run_command. Must never
    raise: it runs inside the agent loop AFTER the assistant message (with
    its tool_use) is already in history — an exception here (binary target,
    permission error) would kill the turn and leave that tool_use dangling,
    poisoning every later request."""
    try:
        body = _diff_preview(tool, args)
    except Exception as e:
        body = f"[diff unavailable: {e.__class__.__name__}: {e}]"
    return _rewind_note(tool, args) + body


_PATH_TOOLS = ("write_file", "edit_file", "apply_patch")


def _rewind_note(tool: str, args: dict) -> str:
    """R130: warn, at the approval prompt, when a mutation lands OUTSIDE the
    tree /rewind can restore.

    R47 checkpoints the working tree before every approved mutation, but its
    work-tree is the cwd — so a write to `~/Desktop/x.md` or a sibling
    project is snapshotted by nothing, and `/rewind` silently cannot undo
    it. R30's system prompt actively pushes the model toward absolute and
    `~` paths, so this is a normal case, not an exotic one.

    Same principle as R95a: the approval prompt is what buys consent, so it
    must not imply an undo guarantee that doesn't exist. A note here, not a
    refusal — writing outside the project is legitimate; being told it is
    unrecoverable is the point. `run_command` is deliberately excluded: what
    a shell command touches isn't knowable from its arguments, so a note
    keyed on them would be guesswork in both directions."""
    if tool not in _PATH_TOOLS:
        return ""
    path = args.get("path", "")
    if not path or rewind.covers(path):
        return ""
    return ("[note: outside the checkpointed tree — /rewind cannot undo "
            "this write]\n")


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
