"""R439: the page's Markdown (chat replies, Notes View, previews) covers the CommonMark/GFM people and models write,
never builds HTML from the text, and never loses the text or hangs."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser  # noqa: E402,F401  (fixture)

R = """md => { const d = document.createElement('div'); mdRender(d, md); return d.innerHTML; }"""


@pytest.fixture
def page(browser, app):
    p = browser.new_page(); p.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    p.goto(app.base + "/"); p.wait_for_selector("#model:not(:empty)")
    yield p
    p.close()


def md(page, s):
    return page.evaluate(R, s)


CASES = [
    ("# H1\n###### H6 ##", "<h1>H1</h1><h6>H6</h6>"),
    ("Title\n===", "<h1>Title</h1>"),
    ("#nospace", "<p>#nospace</p>"),
    ("~~~py\nx = 1\n~~~", '<pre data-lang="py"><code class="language-py">x = 1</code></pre>'),
    ("````\n```\ninner\n```\n````", "<pre><code>```\ninner\n```</code></pre>"),
    ("```\nopen to the end", "<pre><code>open to the end</code></pre>"),
    ("text\n\n    indented code\n    line 2", "<p>text</p><pre><code>indented code\nline 2</code></pre>"),
    ("> quote\n> - item\n>\n> ```\n> code\n> ```", "<blockquote><p>quote</p><ul><li>item</li></ul><pre><code>code</code></pre></blockquote>"),
    ("> lazy\ncontinued", "<blockquote><p>lazy\ncontinued</p></blockquote>"),
    ("- a\n  - b\n    - c\n- d", "<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li><li>d</li></ul>"),
    ("1. one\n  - sub\n2. two", "<ol><li>one<ul><li>sub</li></ul></li><li>two</li></ol>"),
    ("3. three\n4. four", '<ol start="3"><li>three</li><li>four</li></ol>'),
    ("- a\n\n- b", '<ul class="loose"><li><p>a</p></li><li><p>b</p></li></ul>'),
    ("- a\n- b\n1. c", "<ul><li>a</li><li>b</li></ul><ol><li>c</li></ol>"),
    ("Steps:\n1. go\n2. stop", "<p>Steps:</p><ol><li>go</li><li>stop</li></ol>"),
    ("**Tags:**\n| A | B |\n|:--|--:|\n| 1 | 2 |", '<p><strong>Tags:</strong></p><table><thead><tr><th style="text-align: left;">A</th><th style="text-align: right;">B</th></tr></thead><tbody><tr><td style="text-align: left;">1</td><td style="text-align: right;">2</td></tr></tbody></table>'),
    ("a | b\n--|--\n`x|y` | c\\|d", "<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td><code>x|y</code></td><td>c|d</td></tr></tbody></table>"),
    ("| a | b |\n|---|---|\n| only one |", "<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>only one</td><td></td></tr></tbody></table>"),
    ("x | y\n---", "<p>x | y</p><hr>"),
    ("* * *\n___", "<hr><hr>"),
    ("***both*** **b** __b__ *i* _i_ ~~s~~", "<p><strong><em>both</em></strong> <strong>b</strong> <strong>b</strong> <em>i</em> <em>i</em> <del>s</del></p>"),
    ("snake_case_name and 2*3*4", "<p>snake_case_name and 2<em>3</em>4</p>"),
    ("`` a`b `` and ```x```", "<p><code>a`b</code> and <code>x</code></p>"),
    ("\\*not em\\* \\# \\[x]", "<p>*not em* # [x]</p>"),
    ("&amp; &lt;b&gt; &#169; &copy;", "<p>&amp; &lt;b&gt; © &amp;copy;</p>"),
    ("line<br>next  \nhard\\\nend", "<p>line<br>next<br>hard<br>end</p>"),
]


def test_r439_blocks_and_inline(page):
    bad = [(src, got, want) for src, want in CASES if (got := md(page, src)) != want]
    assert not bad, "\n\n".join(f"{s!r}\n got  {g}\n want {w}" for s, g, w in bad)


def test_r439_links(page):
    h = md(page, '[site](https://a.b/c_(d) "T") <https://x.y> see https://e.f/g. and www.h.io, [m](mailto:a@b.c) ![pic](https://p.q/r.png)')
    assert 'href="https://a.b/c_(d)" target="_blank" rel="noopener noreferrer" title="T">site</a>' in h
    assert '>https://x.y</a>' in h and 'href="https://e.f/g"' in h and 'href="https://www.h.io"' in h and 'href="mailto:a@b.c"' in h
    assert 'class="md-img"' in h and "<img" not in h                                  # pictures are links: nothing loads


def test_r439_nothing_becomes_html(page):
    h = md(page, '<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1)) [y](data:text/html,1) <a href="javascript:1">z</a>\n\n| <b>t</b> |\n|---|\n| <i>u</i> |')
    assert "<script" not in h and "<img" not in h and '<a href="javascript' not in h and 'href="data:' not in h
    assert "<b>" not in h and "<i>" not in h and "&lt;script&gt;" in h and "&lt;a href=" in h   # shown as text
    assert " x y " in h                                                                   # the words stay, the target goes


def test_r439_tasks_keep_their_source_line_for_notes(page):
    src = "intro\n\n- [ ] a\n  - [x] nested\n- [X] b\n\n> - [ ] quoted"
    lines = page.evaluate("""md => { const d = document.createElement('div'), got = [];
        mdRender(d, md, { task: (line, on) => got.push([line, on]) });
        d.querySelectorAll('input[type=checkbox]').forEach(cb => { if (!cb.disabled) { cb.checked = !cb.checked; cb.dispatchEvent(new Event('change')); } });
        return [got, d.querySelectorAll('input[type=checkbox]').length, d.querySelectorAll('input[disabled]').length]; }""", src)
    assert lines == [[[2, True], [3, False], [4, False]], 4, 1]                   # a quoted task is shown, not editable


def test_r439_never_hangs_never_throws_never_loses_text(page):
    out = page.evaluate("""() => {
        const pieces = ['*', '**', '_', '`', '```', '~~~', '[', ']', '(', ')', '|', '---', '> ', '- ', '1. ', '  ', '\\n', '\\n\\n', '#', '\\\\', '<', '>', '&amp;', 'http://x.y', 'word', '[ ] ', '\\t', ':-:'];
        let seed = 7; const rnd = n => (seed = (seed * 1103515245 + 12345) % 2147483648) % n;
        const fails = []; const t0 = performance.now();
        for (let k = 0; k < 400; k++) {
          let s = ''; for (let j = 0; j < 60; j++) s += pieces[rnd(pieces.length)];
          const d = document.createElement('div');
          try { mdRender(d, s); } catch (e) { fails.push(String(e)); continue; }
          const plain = s.replace(/^([>\s*+\-\d.)]*)(`{3,}|~{3,})(.*)$/gm, '$1$2');   // a fence's info string is hidden by design
          const words = (plain.match(/word/g) || []).length, shown = (d.textContent.match(/word/g) || []).length;
          if (shown < words) fails.push('lost text: ' + JSON.stringify(s));
        }
        const big = ('para with **bold** and `code` | x\\n'.repeat(4000)) + '*'.repeat(20000);
        const d = document.createElement('div'); const t1 = performance.now(); mdRender(d, big);
        return { fails: fails.slice(0, 5), ms: performance.now() - t0, bigMs: performance.now() - t1 };
    }""")
    assert not out["fails"], out["fails"]
    assert out["bigMs"] < 3000, out


def test_r439_streaming_does_not_split_a_tilde_fence_or_a_list(page):
    got = page.evaluate("""() => [commitPoint('~~~\\na\\n\\nb', 0), commitPoint('- a\\n\\n- b', 0), commitPoint('para\\n\\nnext', 0)]""")
    assert got == [0, 0, 6]
