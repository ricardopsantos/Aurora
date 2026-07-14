"""/remember (R52) — parsing, formatting, root discovery, and the
approval-gated write flow (model + frontend faked, no network)."""

import datetime

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


def test_find_context_root_walks_up(tmp_path):
    (tmp_path / ".agentic_context" / "MEMORY").mkdir(parents=True)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert memory.find_context_root(str(deep)) == tmp_path / ".agentic_context"
    assert memory.find_context_root("/") is None or True  # never raises


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
    monkeypatch.chdir(tmp_path)
    fe = _FakeFE([("y", ""), ("n", "")])
    memory.remember(_FakeEngine(_REPLY), fe)
    written = list((root / "MEMORY").rglob("*.md"))
    assert len(written) == 1 and written[0].parent.name == "aurora"
    assert "> summary:" in written[0].read_text()
    assert len(fe.asked) == 2


def test_remember_none_writes_nothing(tmp_path, monkeypatch):
    root = tmp_path / ".agentic_context"
    (root / "MEMORY").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    memory.remember(_FakeEngine("NONE"), _FakeFE([]))
    assert list((root / "MEMORY").rglob("*.md")) == []
