"""/commit (R101): stage relevant changes, draft a commit message with the
current model from the staged diff, show it, and commit on approval —
removing the one manual step every coding session ends with.

Operates on the REAL project `.git` — a completely different target from
`rewind.py`'s shadow repo (a parallel, separate git history under
AURORA_HOME used purely for undo). `rewind.checkpoint()`/`restore()` must
never be confused with anything here; neither module imports the other.

Engine-side module: no UI imports. Drafting a message needs the engine (to
run a one-off model completion), so `draft_message` takes it as a plain
argument the same way `memory._draft()` does — this module itself never
imports `engine.py`.
"""

import subprocess

from .providers.base import side_completion

_DRAFT_PROMPT = """\
Write a git commit message for this diff. Follow the style of the repo's \
own recent commits (below): concise, explains WHY not just WHAT, no AI \
attribution, no trailing period on the summary line. Reply with ONLY the \
commit message text — no commentary, no quotes, no markdown fences.

Recent commit messages (style reference):
{recent}

Diff:
{diff}
"""


class GitError(Exception):
    """R230: a git operation this module could not complete.

    Every other function here is deliberately non-raising (`check=False`,
    or a blanket `except` returning a text result) so a slash command can
    never die on a git hiccup. `stage_all` is the exception — it MUTATES the
    index, so silently swallowing a failure would leave `/commit` reporting
    success on an empty stage — and it used to leak `git`'s own
    `CalledProcessError`/`TimeoutExpired` straight through an unguarded call
    site. This class existed but was never raised anywhere; it is what
    `stage_all` raises now, so callers have one thing to catch."""


# R125d: cap what actually goes to the model, independent of ui.py's
# display-only _COMMIT_DIFF_PREVIEW_CAP — a large staged diff (vendored
# deps, a regenerated lockfile) would otherwise blow a local model's context
# or rack up unexpected remote token cost even though the preview looked
# bounded.
_DRAFT_DIFF_CAP = 20000


def _git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    # R285: decode as UTF-8 with replacement, never the strict locale codec —
    # a staged latin-1 file (or any non-ASCII under LANG=C) raised
    # UnicodeDecodeError out of staged_diff()/unstaged_summary(), which are
    # unguarded, killing /commit with a raw exception.
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=30, check=check)


def is_repo(cwd: str = ".") -> bool:
    try:
        r = _git(cwd, "rev-parse", "--is-inside-work-tree", check=False)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def staged_diff(cwd: str = ".") -> str:
    """`git diff --staged` — empty string when nothing is staged."""
    r = _git(cwd, "diff", "--staged", check=False)
    return r.stdout


def unstaged_summary(cwd: str = ".") -> str:
    """`git status --porcelain` — what `stage_all` WOULD add. Shown to the
    user before asking whether to auto-stage, so "stage everything and
    commit" is never a silent surprise about which files that includes."""
    r = _git(cwd, "status", "--porcelain", check=False)
    return r.stdout


def stage_all(cwd: str = ".") -> None:
    """`git add -A`. Raises GitError if the index could not be updated —
    a stale `index.lock` from a crashed git, an unreadable path, or a
    timeout on a very large tree are all ordinary, and the caller must not
    then go on to draft and commit as though staging had worked."""
    try:
        r = _git(cwd, "add", "-A", check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(f"git add failed: {e.__class__.__name__}: {e}") from e
    if r.returncode != 0:
        raise GitError("git add failed: "
                       + ((r.stderr or r.stdout).strip()[:300] or
                          f"exit {r.returncode}"))


def recent_log(cwd: str = ".", n: int = 5) -> str:
    """The last `n` commit subjects — style reference for the draft prompt,
    same idea as showing an LLM a few examples before asking for one more."""
    r = _git(cwd, "log", f"-{n}", "--format=%s", check=False)
    return r.stdout


def draft_message(engine, diff: str, recent: str) -> str:
    """One-off model completion, same shape as `memory._draft()` — a plain
    user-turn request outside the normal conversation, not a tool call."""
    shown_diff = diff
    if len(diff) > _DRAFT_DIFF_CAP:
        shown_diff = diff[:_DRAFT_DIFF_CAP] + "\n… (truncated)"
    ask = _DRAFT_PROMPT.format(recent=recent.strip() or "(no history yet)",
                               diff=shown_diff)
    provider = engine._provider_for(engine.current, interactive=True)
    # R255: same side-completion shape as the approval gate's "explain" and
    # the compact summarizer, and it had the same two bugs — the draft's
    # reasoning streamed into the running turn's think row, and a thinking
    # model that answered in `reasoning_content` produced an empty draft
    # (here that reads as "no message could be drafted", which is wrong).
    return side_completion(provider, engine.current.get("model", ""), ask)


def commit(message: str, cwd: str = ".") -> str:
    """Commit currently-staged changes. Returns a short human-readable
    result line — never raises (mirrors `rewind.restore`'s "never break the
    turn/session" contract for a git-shelling operation)."""
    try:
        r = _git(cwd, "commit", "-m", message, check=False)
        if r.returncode != 0:
            return f"commit failed: {(r.stderr or r.stdout).strip()[:300]}"
        rev = _git(cwd, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
        return f"committed {rev}" if rev else "committed"
    except Exception as e:
        return f"commit failed: {e.__class__.__name__}: {e}"
