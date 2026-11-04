"""R436: reply actions are house icon buttons with Read aloud in the row. R437: deep-dive fixes (panels without a
second title bar, notes open/tab changes run in order, export list parser has no shared state, no autofocus)."""
import threading
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "aurora/web"


def test_r436_actions_use_icons_not_glyphs():
    js = (WEB / "app.js").read_text()
    for glyph in ("'↻ Regenerate'", "'✎ Edit'", "'📝 Save as note'", "'📝 Saved'"):
        assert glyph not in js, glyph
    assert "actBtn('Regenerate'" in js and "function paintSpeak(b, on)" in js and "a.append(sp)" in js
    assert "a.querySelectorAll('.speak').forEach(sp => a.before(sp))" in js      # an older reply keeps its Read aloud


def test_r437_panels_have_one_title_and_notes_run_in_order():
    html, js = (WEB / "index_v2.html").read_text(), (WEB / "app.js").read_text()
    assert "#notes.panel .ntop,#files.panel .bar{display:none}" in html
    assert '<div class="nfind">' in html and '<div class="fl-head">' in html
    assert html.index('id="note-back"') > html.index('<div class="nhead">')
    assert "const serial = f => (chain = chain.then(f, f));" in js and "await serial(async () => {" in js
    assert "if (dlg.contains(document.activeElement)) document.activeElement.blur();" in js


def test_r437_export_lists_render_the_same_in_parallel():
    from aurora.web.export import markdown
    src = "- a\n  - b\n    - c\n- d\n\n1. x\n2. y\n"
    want = markdown(src)
    assert want == "<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li><li>d</li></ul><ol><li>x</li><li>y</li></ol>"
    bad = []

    def run():
        for _ in range(300):
            if markdown(src) != want:
                bad.append(1)
    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not bad
