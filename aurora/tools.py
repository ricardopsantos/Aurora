"""The tool set Aurora exposes to the model (R6). Each tool is a spec (name +
JSON-schema parameters, provider-agnostic) plus a `run(args) -> str`.
Writes/commands are gated by the caller (agent.py) via approve.py; the tools
themselves just do the work. Reads and web tools need no approval."""

import subprocess
from pathlib import Path

from . import patch as patchmod

MAX_READ_BYTES = 200_000

# which tools mutate / execute — the caller gates these
NEEDS_APPROVAL = {"write_file", "edit_file", "run_command", "apply_patch",
                 "wait_until"}

# R94: tools the agent loop may run CONCURRENTLY within one round. An
# explicit allowlist, deliberately NOT "everything outside NEEDS_APPROVAL":
# the test is "read-only AND has no shared state", which `todo_write`
# (rewrites the task list) fails even though it's ungated. Everything here
# only reads the filesystem or the network, so ordering between them is
# unobservable — the model asked for all of them at once anyway.
PARALLEL_SAFE = {"read_file", "list_dir", "grep", "open_context_doc",
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


GREP_TIMEOUT = 30


def grep(pattern: str, path: str = ".", **_) -> str:
    try:
        excludes = [f"--exclude-dir={d}" for d in GREP_PRUNE]
        # -E (extended regex), NOT the default BRE: models write ERE by
        # habit — `(foo|bar)`, `a+`, `x?`. Under BRE those metacharacters are
        # literals, so the search SILENTLY returns "[no matches]" instead of
        # erroring, and the model concludes the code doesn't exist (R90b).
        proc = subprocess.Popen(
            ["grep", "-rnIE", *excludes, "--", pattern, str(_resolve(path))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        import select
        import time
        chunks: list[str] = []
        total = 0
        truncated = False
        timed_out = False
        try:
            deadline = time.monotonic() + GREP_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                ready, _, _ = select.select([proc.stdout], [], [], remaining)
                if not ready:
                    continue
                chunk = proc.stdout.read(65536)
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
            stderr = ""
            try:
                stderr = proc.stderr.read(4096) or ""
            except Exception:
                pass
            proc.stdout.close()
            proc.stderr.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if timed_out:
            return f"[grep error: timeout after {GREP_TIMEOUT}s]"
        out = "".join(chunks).strip()
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


def write_file(path: str, content: str, **_) -> str:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"[wrote {len(content)} bytes to {path}]"


def edit_file(path: str, old: str, new: str, replace_all: bool = False,
              **_) -> str:
    """Replace `old` with `new`. Unique-anchor by default; `replace_all`
    (R90g) opts into every occurrence, so renaming a symbol that appears 20×
    is one call instead of 20 uniquely-anchored ones."""
    p = _resolve(path)
    if not p.is_file():
        return f"[error: no such file: {path}]"
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        return "[error: `old` text not found — it must match exactly]"
    if n > 1 and not replace_all:
        return (f"[error: `old` text appears {n} times — make it unique, "
                f"or pass replace_all=true to change all {n}]")
    p.write_text(text.replace(old, new), encoding="utf-8")
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
    text = p.read_text(encoding="utf-8")
    try:
        new_text = patchmod.apply(text, hunks)
    except patchmod.PatchError as e:
        return f"[error: {e}]"
    if new_text == text:
        return "[no changes — patch was a no-op]"
    p.write_text(new_text, encoding="utf-8")
    return f"[applied {len(hunks)} hunk(s) to {path}]"


# default only: run_command's timeout is runtime.timeout when the engine has
# passed one down (set_command_timeout), so a slow build isn't cut off at a
# constant the user can't reach (R90g)
COMMAND_TIMEOUT = 300


def set_command_timeout(seconds: float) -> None:
    global COMMAND_TIMEOUT
    COMMAND_TIMEOUT = max(1, int(seconds))


def _run_command_once(command: str, workdir: str | None) -> tuple[str, int | None]:
    """Run one shell command to completion (or COMMAND_TIMEOUT), process-
    group-safe (R95c). Returns (raw combined stdout+stderr, returncode) —
    returncode is None on a timeout.

    Shared by `run_command` (the tool, which formats this into its
    "[exit N]"/"[timeout ...]" display text) and `wait_until` (R100, which
    needs the REAL exit code to decide whether to keep polling — parsing
    that back out of run_command's own display text would be fragile and
    is exactly the kind of thing that silently breaks the moment the text
    format changes)."""
    # R95c: own the whole process GROUP. `subprocess.run(shell=True,
    # timeout=…)` kills only the shell — every child it spawned survives,
    # reparented to init, and keeps running for the rest of the session (a
    # timed-out build, dev server or test run burns CPU forever). A new
    # session makes the shell a group leader so the timeout can kill the
    # whole tree.
    import os
    proc = subprocess.Popen(command, shell=True, cwd=workdir, text=True,
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
    try:
        stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        _kill_group(proc, pgid)
        # TimeoutExpired carries what was read before the deadline — a
        # partial build log is far more useful than a bare timeout line.
        # (Re-draining via communicate() is unreliable once the group is
        # killed, so take it from the exception.)
        partial = (_text(e.stdout) + _text(e.stderr)).strip()
        try:
            proc.wait(timeout=5)   # reap; never leave a zombie behind
        except subprocess.TimeoutExpired:
            pass
        return partial, None
    return (stdout or "") + (stderr or ""), proc.returncode


def run_command(command: str, cwd: str = "", **_) -> str:
    """Run a shell command, optionally in `cwd` (R90g) — otherwise the model
    has to prefix every call with its own `cd … && …` inside the shell
    string, which breaks as soon as a path needs quoting."""
    workdir = str(_resolve(cwd)) if cwd else None
    if workdir and not Path(workdir).is_dir():
        return f"[error: no such directory: {cwd}]"
    out, code = _run_command_once(command, workdir)
    if code is None:
        head = f"[timeout after {COMMAND_TIMEOUT}s]"
        return f"{out}\n{head}" if out else head
    return (out.strip() or "[no output]") + (f"\n[exit {code}]" if code else "")


def wait_until(command: str, cwd: str = "", interval: float = 2.0,
              timeout: float = 60.0, **_) -> str:
    """Repeatedly run `command` until it exits 0 or `timeout` seconds pass
    (R100) — the same "poll until true or give up" shape
    `llamadesk.LlamaDesk.wait_ready` already uses for a model load, exposed
    as a general tool. Useful for "wait for the dev server to be listening",
    "wait until this file appears", etc. — instead of the model guessing a
    single sleep duration and hoping it was long enough.

    Approval is asked ONCE for the whole call — `agent.py`'s gate wraps the
    tool call itself, not each internal attempt, since re-approving every
    poll would make this unusable. Each attempt reuses `_run_command_once`
    directly (bypassing the approval gate, which already ran for this
    call), same execution as `run_command` — process-group-safe, bounded by
    the shared `COMMAND_TIMEOUT` per attempt."""
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
        out, code = _run_command_once(command, workdir)
        shown = out.strip() or "[no output]"
        if code == 0:
            elapsed = _time.monotonic() - start
            return (f"[wait_until: succeeded after {attempt} attempt(s), "
                    f"{elapsed:.1f}s]\n{shown}")
        if _time.monotonic() - start + interval > timeout:
            status = "timed out mid-command" if code is None else f"exit {code}"
            return (f"[wait_until: gave up after {attempt} attempt(s), "
                    f"~{timeout:.0f}s ({status}) — last output:]\n{shown}")
        _time.sleep(interval)


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
    "write_file": write_file, "edit_file": edit_file, "run_command": run_command,
    "apply_patch": apply_patch, "wait_until": wait_until,
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
                 "description": "directory to run it in (optional)"}},
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
                    "instead of guessing a single sleep duration.",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string"},
         "cwd": {"type": "string", "description": "directory to run it in (optional)"},
         "interval": {"type": "number",
                     "description": "seconds between attempts (default 2)"},
         "timeout": {"type": "number",
                    "description": "give up after this many seconds "
                                   "(default 60, max 300)"}},
         "required": ["command"]}},
]


# R93: offer the task-list tool. On by default; runtime.todo_tool can turn it
# off, for a small local model that gets confused by one more tool more than
# it gains from a plan.
TODO_ENABLED = True


def set_todo_enabled(on: bool) -> None:
    global TODO_ENABLED
    TODO_ENABLED = bool(on)


def specs(include_web: bool) -> list[dict]:
    from . import context, todo, websearch
    s = list(SPEC)
    if include_web:
        s += websearch.SPEC
    if context.active():
        s += context.SPEC
    if TODO_ENABLED:
        s += todo.SPEC
    return s


# Hard cap on what a single tool result feeds the model. 200KB of grep/file
# output is ~50k tokens — one call could evict the whole conversation on a
# 65k local context. ~15k tokens is plenty; the model can re-read narrower.
TOOL_OUTPUT_LIMIT = 60_000


def run_tool(name: str, args: dict) -> str:
    from . import context, todo, websearch
    for table in (RUNNERS, websearch.RUNNERS, context.RUNNERS, todo.RUNNERS):
        if name in table:
            try:
                out = table[name](**args)
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


def run_tools_parallel(calls: list) -> dict[int, str]:
    """Run several PARALLEL_SAFE calls at once (R94). `calls` is a list of
    (index, name, args); returns {index: output}. Every tool here only reads
    the filesystem or the network, and `run_tool` already swallows every
    exception into a `[tool error: …]` string, so a worker can neither raise
    nor corrupt shared state. The CALLER still processes results in the
    original order — approvals, secret challenges and the transcript stay
    strictly sequential, only the waiting overlaps."""
    from concurrent.futures import ThreadPoolExecutor
    if len(calls) < 2:
        return {i: run_tool(n, a) for i, n, a in calls}
    with ThreadPoolExecutor(max_workers=min(len(calls), MAX_PARALLEL)) as pool:
        futures = {i: pool.submit(run_tool, n, a) for i, n, a in calls}
        return {i: f.result() for i, f in futures.items()}
