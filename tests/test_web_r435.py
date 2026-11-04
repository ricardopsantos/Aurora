"""R435: the chat download reads clearly: Markdown answers, numbered prompt bubbles, one 'Worked' block per turn."""
import re

from tests.test_web import app  # noqa: F401  (fixture)
from tests.test_web_r410 import _get
from tests.test_web_r421 import _chat


def test_r435_markdown_is_rendered_and_safe():
    from aurora.web.export import markdown
    h = markdown("# Title\n\nSome **bold**, *it*, `x<y` and [site](https://a.b/c?d=1&e=2).\n\n"
                 "- one\n  - nested\n- [x] done\n\n1. first\n2. second\n\n"
                 "| a | b |\n|---|---|\n| 1 | 2 |\n\n```py\nif a < b:\n    pass\n```\n\n> quoted\n\n---\n"
                 "<script>alert(1)</script> [bad](javascript:alert(1)) <img src=x onerror=y>")
    assert "<h1>Title</h1>" in h and "<strong>bold</strong>" in h and "<em>it</em>" in h and "<code>x&lt;y</code>" in h
    assert '<a href="https://a.b/c?d=1&amp;e=2">site</a>' in h
    assert "<ul><li>one<ul><li>nested</li></ul></li><li>☑ done</li></ul>" in h and "<ol><li>first</li><li>second</li></ol>" in h
    assert "<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>" in h
    assert '<pre><code class="language-py">if a &lt; b:\n    pass</code></pre>' in h
    assert "<blockquote><p>quoted</p></blockquote>" in h and "<hr>" in h
    assert "<script" not in h and "<img" not in h and 'href="javascript' not in h


def test_r435_turns_bubbles_and_steps(app, tmp_path):
    sid = _chat(app, tmp_path)
    brief = _get(app, f"/api/sessions/{sid}/export?level=brief")[2].decode()
    assert brief.count('<section class="turn">') == 1 and '<div class="bubble md"><p>what is &lt;it&gt;?</p></div>' in brief
    assert "<b>You</b><span>#1</span>" in brief and '<div class="md"><p>The &lt;answer&gt; is 42</p></div>' in brief
    assert "Worked:" not in brief and "1 prompt" in brief
    full = _get(app, f"/api/sessions/{sid}/export?level=full")[2].decode()
    assert "Worked: 3 steps (1 tool call, thinking)" in full
    assert full.index("Worked:") < full.index('<div class="ans">')            # the work comes before its answer
    assert re.search(r"<script|<link|src=", full) is None


def test_r435_long_prompt_folds_and_title_is_plain():
    from aurora.web.export import _prompt, _plain_title
    long = "# Session bootstrap\n\n" + "\n".join(f"line {i}" for i in range(40))
    h = _prompt(long)
    assert h.startswith('<details class="bubble long">') and "Show all 42 lines" in h and "<h1>Session bootstrap</h1>" in h
    assert _plain_title("# Session `<root>` **bootstrap**\nmore") == "Session <root> bootstrap"


def test_r435_tool_step_title_is_the_tool_and_its_target():
    from aurora.web.export import _call
    assert _call("run_command", {"command": "ls -la /tmp"}) == ("run_command: ls -la /tmp", "ls -la /tmp")
    head, full = _call("read_file", '{"path": "/a/b.txt", "limit": 5}')
    assert head == "read_file: /a/b.txt" and '"limit": 5' in full
    assert _call("discover_root", None) == ("discover_root", "")
