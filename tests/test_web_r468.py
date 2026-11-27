"""R468: the note editor's toolbar holds only buttons; the save status is in the footer, a confirmation is a toast
(owner, 2026-11-24: "Link copied: open it to start Aurora…" stood one word per line beside the buttons)."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "aurora" / "web"
HTML = (WEB / "index_v2.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")


def test_toolbar_has_no_status_text_and_the_footer_has_it():
    head = re.search(r'<div class="nhead">(.*?)</div>', HTML, re.S).group(1)
    assert 'id="note-state"' not in head
    foot = re.search(r'<div class="nfoot">(.*?)</div>', HTML, re.S).group(1)
    assert 'id="note-meta"' in foot and 'id="note-state"' in foot
    assert 'id="note-toast"' in HTML and "#note-toast[hidden]{display:none}" in HTML


def test_confirmations_are_short_toasts():
    assert "Link copied: open it" not in JS
    assert "flash('Link copied')" in JS and "flash('Copied')" in JS and "flash('Moved to '" in JS
    assert "'Link: ' + url" not in JS                     # never a long URL dumped in the status
