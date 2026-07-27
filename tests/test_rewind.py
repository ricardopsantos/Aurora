"""Checkpoints + /rewind (R47): snapshot before mutations, restore, undo."""

import pytest

from aurora import agent, rewind


@pytest.fixture
def proj(tmp_path, monkeypatch):
    # isolate the shadow repos under a throwaway AURORA_HOME
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    d = tmp_path / "proj"
    d.mkdir()
    (d / "a.txt").write_text("v1")
    return d


def test_checkpoint_restore_roundtrip(proj):
    cid = rewind.checkpoint("write a.txt", cwd=str(proj))
    assert cid
    # mutate: change a tracked file AND create a new one
    (proj / "a.txt").write_text("v2")
    (proj / "junk.txt").write_text("oops")
    assert "restored" in rewind.restore(cid, cwd=str(proj))
    assert (proj / "a.txt").read_text() == "v1"
    assert not (proj / "junk.txt").exists()


def test_checkpoint_dedups_unchanged_tree(proj):
    assert rewind.checkpoint("first", cwd=str(proj))
    assert rewind.checkpoint("same tree", cwd=str(proj)) is None


def test_rewind_is_undoable(proj):
    cid = rewind.checkpoint("before", cwd=str(proj))
    (proj / "a.txt").write_text("v2")
    msg = rewind.restore(cid, cwd=str(proj))
    assert (proj / "a.txt").read_text() == "v1"
    undo = msg.split("/rewind ")[-1].rstrip(")")  # "(undo with /rewind <id>)"
    assert "restored" in rewind.restore(undo, cwd=str(proj))
    assert (proj / "a.txt").read_text() == "v2"


def test_entries_show_labels_newest_first(proj):
    rewind.checkpoint("[write_file] make the thing", cwd=str(proj))
    (proj / "a.txt").write_text("v2")
    rewind.checkpoint("[run_command] test the thing", cwd=str(proj))
    rows = rewind.entries(cwd=str(proj))
    assert [r["label"] for r in rows] == [
        "[run_command] test the thing", "[write_file] make the thing"]


def test_gitignored_files_survive_a_rewind(proj):
    (proj / ".gitignore").write_text("secret.env\n")
    (proj / "secret.env").write_text("KEY=1")
    cid = rewind.checkpoint("before", cwd=str(proj))
    (proj / "a.txt").write_text("v2")
    rewind.restore(cid, cwd=str(proj))
    assert (proj / "secret.env").read_text() == "KEY=1"  # never tracked/cleaned


def test_restore_bad_ref_is_a_message_not_a_crash(proj):
    rewind.checkpoint("x", cwd=str(proj))
    assert "no such checkpoint" in rewind.restore("deadbeef", cwd=str(proj))


# ── /diff (feature: show what the last turn actually changed) ──────────────
def test_head_is_none_before_any_checkpoint(proj):
    assert rewind.head(cwd=str(proj)) is None


def test_head_returns_the_current_checkpoint(proj):
    cid = rewind.checkpoint("first", cwd=str(proj))
    assert rewind.head(cwd=str(proj)) == cid


def test_diff_since_shows_changes_made_after_the_given_checkpoint(proj):
    base = rewind.checkpoint("before", cwd=str(proj))
    (proj / "a.txt").write_text("v2")
    rewind.checkpoint("[write_file] change a.txt", cwd=str(proj))
    diff = rewind.diff_since(base, cwd=str(proj))
    assert "-v1" in diff and "+v2" in diff


def test_diff_since_none_diffs_against_the_empty_tree(proj):
    """ref=None (no checkpoint existed when the turn started) must still
    show a real diff for a project's very first mutation, not nothing."""
    rewind.checkpoint("[write_file] first ever mutation", cwd=str(proj))
    diff = rewind.diff_since(None, cwd=str(proj))
    assert "+v1" in diff


def test_diff_since_bad_ref_is_a_message_not_a_crash(proj):
    rewind.checkpoint("x", cwd=str(proj))
    assert "no such checkpoint" in rewind.diff_since("deadbeef", cwd=str(proj))


def test_diff_since_is_empty_when_nothing_changed(proj):
    base = rewind.checkpoint("before", cwd=str(proj))
    assert rewind.diff_since(base, cwd=str(proj)) == ""


def test_diff_since_shows_a_new_untracked_file(proj):
    """P-4: checkpoint() runs BEFORE a mutation, so a turn whose only/last
    mutation is a brand-new file leaves it untracked relative to `base` —
    plain `git diff base --` never shows untracked files, so /diff read as
    "turn did nothing" exactly when the user most wants to see the new
    file. No second checkpoint follows here, mirroring a real turn."""
    base = rewind.checkpoint("before", cwd=str(proj))
    (proj / "brand_new.txt").write_text("hello")
    diff = rewind.diff_since(base, cwd=str(proj))
    assert "brand_new.txt" in diff and "+hello" in diff


# ── /undo (feature: revert just the last mutation, not the whole tree) ─────
def test_undo_reverts_uncommitted_new_file(proj):
    """Common case: nothing checkpointed since the last mutation ran — the
    dirty state IS that mutation (R47's invariant), so /undo just discards
    it, leaving earlier checkpointed work untouched."""
    rewind.checkpoint("before", cwd=str(proj))
    (proj / "brand_new.txt").write_text("oops")
    msg = rewind.undo(cwd=str(proj))
    assert "undone" in msg and "brand_new.txt" in msg
    assert not (proj / "brand_new.txt").exists()
    assert (proj / "a.txt").read_text() == "v1"


def test_undo_reverts_uncommitted_modification(proj):
    rewind.checkpoint("before", cwd=str(proj))
    (proj / "a.txt").write_text("v2")
    msg = rewind.undo(cwd=str(proj))
    assert "undone" in msg
    assert (proj / "a.txt").read_text() == "v1"


def test_undo_never_reaches_into_already_sealed_history(proj):
    """The bug that shipped twice: an earlier version of undo() fell back to
    reverting HEAD's own diff against HEAD~1 whenever the tree was clean —
    treating an OLDER, already-sealed, unrelated mutation as if it were "the
    last one". It never legitimately can be: checkpoint() runs BEFORE a
    mutation, never after, so the mutation that just ran — if it touched
    this tree at all — is ALWAYS still sitting uncommitted right now;
    nothing seals it until the NEXT approved mutation's own pre-checkpoint
    runs. A clean tree against HEAD means the last action left nothing
    here, full stop — reaching further back is never correct. mutation 1
    (already sealed into HEAD by mutation 2's own checkpoint call) must
    survive a call to /undo untouched."""
    rewind.checkpoint("m0", cwd=str(proj))
    (proj / "a.txt").write_text("v2")                 # mutation 1
    rewind.checkpoint("m1", cwd=str(proj))             # seals mutation 1
    # nothing else has happened since — the tree is clean against HEAD
    assert rewind.undo_preview(cwd=str(proj)) == ("none", [])
    msg = rewind.undo(cwd=str(proj))
    assert "nothing to undo" in msg
    assert (proj / "a.txt").read_text() == "v2"        # mutation 1 UNTOUCHED


def test_undo_with_nothing_to_undo(proj):
    rewind.checkpoint("before", cwd=str(proj))
    assert "nothing to undo" in rewind.undo(cwd=str(proj))


def test_undo_no_checkpoints_at_all(proj):
    assert "no checkpoints" in rewind.undo(cwd=str(proj))


def test_undo_preview_reports_uncommitted_paths_without_touching_anything(proj):
    rewind.checkpoint("before", cwd=str(proj))
    (proj / "a.txt").write_text("v2")
    kind, paths = rewind.undo_preview(cwd=str(proj))
    assert kind == "uncommitted" and paths == ["a.txt"]
    assert (proj / "a.txt").read_text() == "v2"   # preview changed nothing


def test_undo_preview_none_when_nothing_to_undo(proj):
    rewind.checkpoint("before", cwd=str(proj))
    assert rewind.undo_preview(cwd=str(proj)) == ("none", [])


# ── undo_diff (feature: show the diff alongside the confirm) ───────────────
def test_undo_diff_for_an_uncommitted_tree_change(proj):
    rewind.checkpoint("before", cwd=str(proj))
    (proj / "a.txt").write_text("v2")
    diff = rewind.undo_diff(cwd=str(proj))
    assert "-v1" in diff and "+v2" in diff


def test_undo_diff_for_an_out_of_tree_file_snapshot(proj, tmp_path):
    outside = tmp_path / "notes.txt"
    outside.write_text("original")
    rewind.snapshot_before_write(str(outside), cwd=str(proj))
    outside.write_text("edited")
    diff = rewind.undo_diff(cwd=str(proj))
    assert "-original" in diff and "+edited" in diff


def test_undo_diff_for_a_brand_new_file_shows_a_pure_addition(proj, tmp_path):
    outside = tmp_path / "new.txt"
    rewind.snapshot_before_write(str(outside), cwd=str(proj))   # didn't exist yet
    outside.write_text("created")
    diff = rewind.undo_diff(cwd=str(proj))
    assert "+created" in diff
    content_lines = [l for l in diff.splitlines() if not l.startswith(("---", "+++", "@@"))]
    assert all(l.startswith("+") for l in content_lines)   # no removed lines — pure addition


def test_undo_diff_empty_when_nothing_to_undo(proj):
    rewind.checkpoint("before", cwd=str(proj))
    assert rewind.undo_diff(cwd=str(proj)) == ""


def test_undo_after_an_out_of_tree_mutation_reports_nothing_to_undo(proj, tmp_path):
    """Regression for the actual incident: a mutation whose target is
    OUTSIDE the checkpointed tree (`covers()`'s documented gap — e.g. an
    absolute path elsewhere on disk) leaves this shadow repo with nothing
    to show for it. The correct, safe answer is "nothing to undo" — NOT
    silently reverting the PREVIOUS, unrelated, already-sealed in-tree
    mutation, which is what actually happened in production twice before
    this fix."""
    rewind.checkpoint("m0", cwd=str(proj))
    (proj / "a.txt").write_text("v2")                  # in-tree mutation
    rewind.checkpoint("[write_file] in-tree change", cwd=str(proj))  # seals it

    # the "current" mutation writes somewhere ENTIRELY outside `proj` —
    # checkpoint() still runs (R47 fires unconditionally) but `git add -A`
    # inside `proj` sees nothing, so it returns None
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("out of tree")
    assert rewind.checkpoint("[write_file] outside the tree", cwd=str(proj)) is None

    assert rewind.undo_preview(cwd=str(proj)) == ("none", [])
    msg = rewind.undo(cwd=str(proj))
    assert "nothing to undo" in msg
    assert (proj / "a.txt").read_text() == "v2"   # the EARLIER in-tree change survives
    assert outside.read_text() == "out of tree"   # the out-of-tree file never touched either way


def test_agent_checkpoints_before_mutations_only(proj):
    """run_turn snapshots before an approved write_file, never before reads."""
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb

    snaps = []
    cb = _cb()
    cb.checkpoint = lambda tool, args: snaps.append(tool)
    prov = FakeProvider([
        TurnResult(text="", stop_reason="tool_use", tool_calls=[
            ToolCall("1", "read_file", {"path": str(proj / "a.txt")}),
            ToolCall("2", "write_file", {"path": str(proj / "b.txt"),
                                         "content": "hi"})]),
        TurnResult(text="done", stop_reason="end"),
    ])
    agent.run_turn(prov, "m", [{"role": "user", "content": "go"}],
                   "sys", cb, 5, True)
    assert snaps == ["write_file"]


def test_agent_passes_call_arguments_to_the_checkpoint_callback(proj):
    """R181: the checkpoint callback needs the call's arguments (not just
    its name) to snapshot the target file — `engine.py`'s real callback
    reads `args["path"]`."""
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb

    seen = []
    cb = _cb()
    cb.checkpoint = lambda tool, args: seen.append((tool, args))
    prov = FakeProvider([
        TurnResult(text="", stop_reason="tool_use", tool_calls=[
            ToolCall("1", "write_file", {"path": str(proj / "b.txt"),
                                         "content": "hi"})]),
        TurnResult(text="done", stop_reason="end"),
    ])
    agent.run_turn(prov, "m", [{"role": "user", "content": "go"}],
                   "sys", cb, 5, True)
    assert seen == [("write_file", {"path": str(proj / "b.txt"), "content": "hi"})]


# ── R181: /undo works on a file OUTSIDE the checkpointed tree ──────────────
def test_snapshot_before_write_then_undo_restores_an_out_of_tree_file(proj, tmp_path):
    """The actual incident: editing a file that lives ENTIRELY outside the
    project (a Desktop file) must still be undoable — the whole-tree
    checkpoint can't see it at all (`covers()`'s gap), so this is a
    separate, location-independent mechanism."""
    outside = tmp_path / "Desktop" / "notes.txt"
    outside.parent.mkdir()
    outside.write_text("original")
    rewind.snapshot_before_write(str(outside), cwd=str(proj))
    outside.write_text("edited by the model")

    kind, paths = rewind.undo_preview(cwd=str(proj))
    assert kind == "file" and paths == [str(outside)]
    msg = rewind.undo(cwd=str(proj))
    assert "undone" in msg and str(outside) in msg
    assert outside.read_text() == "original"


def test_snapshot_before_write_of_a_brand_new_file_deletes_it_on_undo(proj, tmp_path):
    outside = tmp_path / "new.txt"
    rewind.snapshot_before_write(str(outside), cwd=str(proj))   # didn't exist yet
    outside.write_text("created by the model")
    assert rewind.undo_preview(cwd=str(proj)) == ("file", [str(outside)])
    rewind.undo(cwd=str(proj))
    assert not outside.exists()


def test_a_non_file_mutation_invalidates_a_stale_file_snapshot(proj):
    """clear_last_mutation() must fire for run_command/wait_until — the R181
    file-snapshot from an EARLIER write_file must not be mistaken for "the
    last mutation" once something else has run since. Falls through to the
    ordinary uncommitted-tree-diff check instead."""
    (proj / "a.txt").write_text("orig")
    rewind.checkpoint("before", cwd=str(proj))
    rewind.snapshot_before_write(str(proj / "a.txt"), cwd=str(proj))
    (proj / "a.txt").write_text("edited")
    rewind.clear_last_mutation(cwd=str(proj))   # a run_command ran after the edit
    (proj / "b.txt").write_text("new")          # the run_command's own effect

    kind, paths = rewind.undo_preview(cwd=str(proj))
    assert kind == "uncommitted"
    assert "b.txt" in paths and "a.txt" in paths   # both uncommitted changes, tree-wide


def test_undo_button_visibility_covers_out_of_tree_snapshots(proj, tmp_path):
    """R181: the status-bar button must appear even when the ONLY thing
    tracked is a per-file snapshot — `rewind.head()` alone stays None
    forever for a project whose only mutation ever was out-of-tree."""
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("v1")
    rewind.snapshot_before_write(str(outside), cwd=str(proj))
    assert rewind.head(cwd=str(proj)) is None       # the old signal stays blind
    assert rewind.undo_preview(cwd=str(proj))[0] == "file"   # the new one isn't


# ── R151: retention — the shadow repo is bounded now ──────────────────────
def _commit_count(proj):
    import subprocess
    gd = rewind._gitdir(proj.resolve())
    return len(subprocess.run(
        ["git", "--git-dir", str(gd), "rev-list", "HEAD"],
        capture_output=True, text=True).stdout.split())


def _undo_tags(proj):
    import subprocess
    gd = rewind._gitdir(proj.resolve())
    return subprocess.run(
        ["git", "--git-dir", str(gd), "tag", "--list", "undo-*"],
        capture_output=True, text=True).stdout.split()


def test_prune_bounds_the_history_and_rewind_still_works(proj):
    """R151: one commit per approved mutation, forever, with no ceiling and
    no way to prune from inside Aurora or out."""
    for i in range(30):
        (proj / "a.txt").write_text("x" * (i + 1) * 200)
        rewind.checkpoint(f"step {i}", cwd=str(proj))
    assert _commit_count(proj) == 30

    cut = rewind.prune(cwd=str(proj), keep=10)
    assert cut == 20
    assert _commit_count(proj) == 10
    # the surviving snapshots are still listable AND restorable — a bounded
    # repo is worthless if it breaks the feature it exists for
    ents = rewind.entries(cwd=str(proj), limit=5)
    assert ents
    assert "restored" in rewind.restore(ents[2]["id"], cwd=str(proj))


def test_prune_drops_the_undo_tags_that_pinned_orphaned_commits(proj):
    """The nastier half: every /rewind left a permanent `undo-<hash>` tag
    making an orphaned commit reachable, so no gc anywhere could reclaim it.
    Growth was irreducible, not merely unbounded."""
    for i in range(12):
        (proj / "a.txt").write_text("y" * (i + 1) * 200)
        rewind.checkpoint(f"step {i}", cwd=str(proj))
    # each rewind to a DISTINCT point leaves its own undo tag
    for i in range(8):
        ents = rewind.entries(cwd=str(proj), limit=20)
        rewind.restore(ents[min(3 + i, len(ents) - 1)]["id"], cwd=str(proj))
        (proj / "a.txt").write_text(f"drift {i}")
        rewind.checkpoint(f"post-rewind {i}", cwd=str(proj))
    assert len(_undo_tags(proj)) > rewind.UNDO_TAG_RETENTION

    rewind.prune(cwd=str(proj), keep=rewind.RETENTION)
    assert len(_undo_tags(proj)) == rewind.UNDO_TAG_RETENTION


def test_prune_is_a_noop_for_a_project_with_no_checkpoints(tmp_path,
                                                           monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    fresh = tmp_path / "untouched"
    fresh.mkdir()
    assert rewind.prune(cwd=str(fresh)) == 0


def test_prune_never_raises_and_never_breaks_checkpointing(proj, monkeypatch):
    """Retention is housekeeping: a failure here must leave the safety net
    itself working, since checkpoint() runs on the approval path."""
    monkeypatch.setattr(rewind, "_git",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert rewind.prune(cwd=str(proj)) == 0


def test_checkpoint_schedules_a_prune_only_every_nth_call(proj, monkeypatch):
    """prune() runs a `git gc`, so it must stay off the approval path — it is
    rate-limited and dispatched to a daemon thread, never run inline."""
    calls = []
    monkeypatch.setattr(rewind, "prune",
                        lambda cwd, *a, **k: calls.append(cwd))
    monkeypatch.setattr(rewind, "_since_prune", 0)
    monkeypatch.setattr(rewind, "_PRUNE_EVERY", 4)

    import threading
    started = []
    real_thread = threading.Thread

    def _spy(*a, **k):
        t = real_thread(*a, **k)
        started.append(t)
        return t

    monkeypatch.setattr(rewind.threading, "Thread", _spy)
    for i in range(4):
        (proj / "a.txt").write_text(f"v{i}")
        rewind.checkpoint(f"s{i}", cwd=str(proj))
    for t in started:
        t.join(timeout=5)
    assert len(started) == 1, "prune should fire once per _PRUNE_EVERY commits"
    assert calls == [str(proj.resolve())]
