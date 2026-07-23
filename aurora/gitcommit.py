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
    pass


def _git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=30, check=check)


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
    _git(cwd, "add", "-A")


def recent_log(cwd: str = ".", n: int = 5) -> str:
    """The last `n` commit subjects — style reference for the draft prompt,
    same idea as showing an LLM a few examples before asking for one more."""
    r = _git(cwd, "log", f"-{n}", "--format=%s", check=False)
    return r.stdout


def draft_message(engine, diff: str, recent: str) -> str:
    """One-off model completion, same shape as `memory._draft()` — a plain
    user-turn request outside the normal conversation, not a tool call."""
    ask = _DRAFT_PROMPT.format(recent=recent.strip() or "(no history yet)",
                               diff=diff)
    msg = [{"role": "user", "content": ask}]
    provider = engine._provider_for(engine.current, interactive=True)
    result = provider.turn(engine.current.get("model", ""), msg, "", None,
                           lambda _s: None, lambda: False)
    return (result.text or "").strip()


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
