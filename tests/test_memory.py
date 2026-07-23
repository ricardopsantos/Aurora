"""/remember (R52) — parsing, formatting, root discovery, and the
approval-gated write flow (model + frontend faked, no network)."""

import datetime
from pathlib import Path

from aurora import memory

_REPLY = """GROUP: aurora
TITLE: Esc owns cancel
SUMMARY: Esc is the single control key; Ctrl+C only clears input.
BODY:
## Finding
Keys must never quit instantly.
===
GROUP: New Stuff!
TITLE: Second one
SUMMARY: also fine
BODY:
## Finding
x
"""


def test_parse_findings_two_blocks():
    out = memory.parse_findings(_REPLY)
    assert len(out) == 2
    assert out[0]["group"] == "aurora"
    assert out[1]["group"] == "new-stuff"          # slugged
    assert out[0]["body"].startswith("## Finding")


def test_parse_none_and_garbage():
    assert memory.parse_findings("NONE") == []
    assert memory.parse_findings("no blocks here") == []
    assert memory.parse_findings("TITLE: x\nBODY:\ny") == []   # no summary


def test_render_finding_house_format():
    when = datetime.datetime(2026, 7, 10, 23, 0, 0)
    rel, text = memory.render_finding(
        {"group": "aurora", "title": "A Thing!", "summary": "s", "body": "b"},
        when)
    assert rel == "MEMORY/aurora/20260710_230000_a-thing.md"
    lines = text.splitlines()
    assert lines[0] == "# A Thing!"
    assert lines[1] == "> summary: s"              # mandatory line 2
    assert "**Discovered:** 2026-07-10" in text


def _make_context_dir(base, name=".agentic_context"):
    root = base / name
    (root / "MEMORY").mkdir(parents=True)
    (root / "KNOWLEDGE").mkdir(parents=True)
    (root / "MEMORY" / "SKILL.md").write_text("# memory skill\n")
    (root / "KNOWLEDGE" / "SKILL.md").write_text("# knowledge skill\n")
    return root


def test_find_context_root_walks_up(tmp_path):
    root = _make_context_dir(tmp_path)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert memory.find_context_root(str(deep)) == root
    assert memory.find_context_root("/") is None or True  # never raises


def test_find_context_root_ignores_the_folder_name(tmp_path):
    # identified by contents (KNOWLEDGE/SKILL.md + MEMORY/SKILL.md), never
    # by being named ".agentic_context" specifically
    root = _make_context_dir(tmp_path, name="my_weird_context_dir")
    assert memory.find_context_root(str(tmp_path)) == root


def test_find_context_root_requires_both_subfolders(tmp_path):
    # a bare MEMORY/ with no KNOWLEDGE/ isn't the real protocol dir
    (tmp_path / ".agentic_context" / "MEMORY").mkdir(parents=True)
    (tmp_path / ".agentic_context" / "MEMORY" / "SKILL.md").write_text("x")
    assert memory.find_context_root(str(tmp_path)) is None


def test_find_context_root_requires_skill_md_in_each(tmp_path):
    # dirs exist but no SKILL.md inside them — not the real protocol dir
    (tmp_path / ".agentic_context" / "MEMORY").mkdir(parents=True)
    (tmp_path / ".agentic_context" / "KNOWLEDGE").mkdir(parents=True)
    assert memory.find_context_root(str(tmp_path)) is None


def test_last_k_messages_slices_at_user_boundary():
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2a"},
        {"role": "tool", "content": "tool result"},
        {"role": "assistant", "content": "a2b"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
    ]
    assert memory._last_k_messages(msgs, 1) == msgs[6:]
    assert memory._last_k_messages(msgs, 2) == msgs[2:]
    assert memory._last_k_messages(msgs, 99) == msgs      # more than exist
    assert memory._last_k_messages([], 1) == []


def test_parse_scope():
    assert memory._parse_scope("") == ("last", 1)
    assert memory._parse_scope("last") == ("last", 1)
    assert memory._parse_scope("last 3") == ("last", 3)
    assert memory._parse_scope("all") == ("all", 0)
    assert memory._parse_scope("last abc") is None
    assert memory._parse_scope("bogus") is None
    assert memory._parse_scope("last 0") is None


class _FakeEngine:
    messages = [{"role": "user", "content": "hi"}]
    current = {"model": "m"}

    def __init__(self, reply):
        self._reply = reply

    def provider_kind(self, *_):
        return "openai"

    def _provider_for(self, *a, **k):
        eng = self

        class P:
            def turn(self, *a, **k):
                from aurora.providers.base import TurnResult
                return TurnResult(text=eng._reply)
        return P()

    class session:
        logged = {}

        @classmethod
        def log(cls, kind, **kw):
            cls.logged = {"kind": kind, **kw}


class _FakeFE:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []
        self.notices = []

    def notify(self, message):
        self.notices.append(message)

    def approve(self, tool, args, diff):
        self.asked.append(args["path"])
        return self.answers.pop(0)


def test_remember_writes_approved_skips_denied(tmp_path, monkeypatch, capsys):
    root = tmp_path / ".agentic_context"
    (root / "MEMORY" / "aurora").mkdir(parents=True)
    (root / "KNOWLEDGE").mkdir(parents=True)
    (root / "MEMORY" / "SKILL.md").write_text("x")
    (root / "KNOWLEDGE" / "SKILL.md").write_text("x")
    monkeypatch.chdir(tmp_path)
    fe = _FakeFE([("y", ""), ("n", "")])
    memory.remember(_FakeEngine(_REPLY), fe)
    written = [p for p in (root / "MEMORY").rglob("*.md") if p.name != "SKILL.md"]
    assert len(written) == 1 and written[0].parent.name == "aurora"
    assert "> summary:" in written[0].read_text()
    assert len(fe.asked) == 2


def test_remember_none_writes_nothing(tmp_path, monkeypatch):
    root = tmp_path / ".agentic_context"
    (root / "MEMORY").mkdir(parents=True)
    (root / "KNOWLEDGE").mkdir(parents=True)
    (root / "MEMORY" / "SKILL.md").write_text("x")
    (root / "KNOWLEDGE" / "SKILL.md").write_text("x")
    monkeypatch.chdir(tmp_path)
    memory.remember(_FakeEngine("NONE"), _FakeFE([]))
    assert [p for p in (root / "MEMORY").rglob("*.md") if p.name != "SKILL.md"] == []


def test_remember_bad_scope_notifies_usage(tmp_path, monkeypatch):
    root = tmp_path / ".agentic_context"
    (root / "MEMORY").mkdir(parents=True)
    (root / "KNOWLEDGE").mkdir(parents=True)
    (root / "MEMORY" / "SKILL.md").write_text("x")
    (root / "KNOWLEDGE" / "SKILL.md").write_text("x")
    monkeypatch.chdir(tmp_path)
    fe = _FakeFE([])
    memory.remember(_FakeEngine(_REPLY), fe, "last abc")
    assert any("usage" in n for n in fe.notices)
    assert [p for p in (root / "MEMORY").rglob("*.md") if p.name != "SKILL.md"] == []


def test_remember_falls_back_to_home_aurora_pfcs_when_no_context(tmp_path, monkeypatch):
    # no .agentic_context at all in tmp_path; fallback is home-anchored,
    # NOT project-anchored — must land under the (fake) home dir, not cwd
    fake_home = tmp_path / "home"
    monkeypatch.setattr(memory.Path, "home", classmethod(lambda cls: fake_home))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    fe = _FakeFE([("y", ""), ("y", "")])
    memory.remember(_FakeEngine(_REPLY), fe, "all")
    fallback = fake_home / "AURORA_PFCS" / "MEMORY"
    written = list(fallback.glob("*.md"))
    assert len(written) == 2                      # flat, no group subfolder
    assert "> summary:" in written[0].read_text()
    assert not (project / ".agentic_context").exists()
    assert not (project / "AURORA_PFCS").exists()  # not project-anchored
    assert any("AURORA_PFCS" in n for n in fe.notices)


def test_fallback_root_is_under_home():
    assert memory._fallback_root() == Path.home() / "AURORA_PFCS" / "MEMORY"


def test_run_stats_missing_script(tmp_path):
    assert "no stats.sh" in memory.run_stats(tmp_path)


def test_run_stats_runs_the_script(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "stats.sh").write_text("#!/bin/bash\necho STATS-OK\n")
    out = memory.run_stats(tmp_path)
    assert "STATS-OK" in out
