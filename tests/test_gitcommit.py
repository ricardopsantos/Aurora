"""/commit (R101): aurora/gitcommit.py — pure git-shelling functions against
a REAL repo (never rewind.py's shadow one)."""

import subprocess

import pytest

from aurora import gitcommit


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=d, check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "test"], check=True)
    (d / "a.txt").write_text("v1\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-m", "initial"], check=True)
    return d


def test_is_repo_true_for_a_real_repo(repo):
    assert gitcommit.is_repo(str(repo))


def test_is_repo_false_for_a_plain_directory(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert not gitcommit.is_repo(str(plain))


def test_staged_diff_is_empty_when_nothing_staged(repo):
    (repo / "a.txt").write_text("v2\n")   # modified but NOT staged
    assert gitcommit.staged_diff(str(repo)) == ""


def test_staged_diff_shows_staged_changes(repo):
    (repo / "a.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    diff = gitcommit.staged_diff(str(repo))
    assert "-v1" in diff and "+v2" in diff


def test_unstaged_summary_lists_modified_and_untracked(repo):
    (repo / "a.txt").write_text("v2\n")
    (repo / "new.txt").write_text("new\n")
    summary = gitcommit.unstaged_summary(str(repo))
    assert "a.txt" in summary and "new.txt" in summary


def test_stage_all_then_staged_diff_sees_everything(repo):
    (repo / "a.txt").write_text("v2\n")
    (repo / "new.txt").write_text("new\n")
    gitcommit.stage_all(str(repo))
    diff = gitcommit.staged_diff(str(repo))
    assert "a.txt" in diff and "new.txt" in diff


def test_recent_log_returns_subject_lines(repo):
    log = gitcommit.recent_log(str(repo), n=5)
    assert "initial" in log


def test_commit_creates_a_real_commit(repo):
    (repo / "a.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    out = gitcommit.commit("update a.txt", str(repo))
    assert out.startswith("committed ")
    log = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert log.strip() == "update a.txt"


def test_commit_with_nothing_staged_fails_cleanly(repo):
    out = gitcommit.commit("nothing to commit here", str(repo))
    assert "failed" in out.lower()


def test_draft_message_calls_the_model_with_the_diff_and_recent_log():
    """draft_message is a plain one-off completion, same shape as
    memory._draft() — verified against a fake engine/provider so no real
    model call is needed."""
    seen = {}

    class _FakeResult:
        text = "fix the thing"

    class _FakeProvider:
        def turn(self, model, messages, system, tools, on_text, cancel):
            seen["prompt"] = messages[0]["content"]
            return _FakeResult()

    class _FakeEngine:
        current = {"model": "m"}

        def _provider_for(self, entry, interactive=True):
            return _FakeProvider()

    out = gitcommit.draft_message(_FakeEngine(), "diff --git a/x b/x\n+hi",
                                  "previous commit subject")
    assert out == "fix the thing"
    assert "diff --git a/x b/x" in seen["prompt"]


def test_draft_message_caps_a_huge_diff_before_sending_to_the_model():
    """R125d: a large staged diff (vendored deps, a regenerated lockfile)
    must not go to the model uncapped — only the *display* preview
    (ui.py's _COMMIT_DIFF_PREVIEW_CAP) was bounded before; the model call
    itself was not."""
    seen = {}

    class _FakeResult:
        text = "ok"

    class _FakeProvider:
        def turn(self, model, messages, system, tools, on_text, cancel):
            seen["prompt"] = messages[0]["content"]
            return _FakeResult()

    class _FakeEngine:
        current = {"model": "m"}

        def _provider_for(self, entry, interactive=True):
            return _FakeProvider()

    huge_diff = "+line\n" * 10_000  # 60,000 chars, well over the cap
    gitcommit.draft_message(_FakeEngine(), huge_diff, "")
    assert len(seen["prompt"]) < len(huge_diff)
    assert "truncated" in seen["prompt"]


# ── R230: stage_all reports failure instead of leaking git's exception ────

def test_stage_all_raises_giterror_when_git_add_fails(tmp_path):
    """R230: not a repo at all — `git add` exits non-zero. This used to
    escape as CalledProcessError through an unguarded /commit call site."""
    with pytest.raises(gitcommit.GitError) as e:
        gitcommit.stage_all(str(tmp_path))
    assert "git add failed" in str(e.value)


def test_stage_all_raises_giterror_when_git_cannot_run(monkeypatch, repo):
    def boom(*a, **k):
        raise OSError("no git binary")
    monkeypatch.setattr(gitcommit.subprocess, "run", boom)
    with pytest.raises(gitcommit.GitError):
        gitcommit.stage_all(str(repo))


def test_stage_all_succeeds_quietly_in_a_real_repo(repo):
    (repo / "new.txt").write_text("x")
    gitcommit.stage_all(str(repo))          # must not raise
    assert "new.txt" in gitcommit.staged_diff(str(repo))
