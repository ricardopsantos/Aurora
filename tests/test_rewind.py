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


def test_agent_checkpoints_before_mutations_only(proj):
    """run_turn snapshots before an approved write_file, never before reads."""
    from aurora.providers.base import ToolCall, TurnResult
    from tests.test_core import FakeProvider, _cb

    snaps = []
    cb = _cb()
    cb.checkpoint = snaps.append
    prov = FakeProvider([
        TurnResult(text="", stop_reason="tool_use", tool_calls=[
            ToolCall("1", "read_file", {"path": str(proj / "a.txt")}),
            ToolCall("2", "write_file", {"path": str(proj / "b.txt"),
                                         "content": "hi"})]),
        TurnResult(text="done", stop_reason="end"),
    ])
    agent.run_turn(prov, "m", [{"role": "user", "content": "go"}],
                   "sys", cb, 5, True, False)
    assert snaps == ["write_file"]
