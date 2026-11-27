"""R443 - the web sidebar and drawer slide instead of vanishing (all three layouts)."""
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "aurora" / "web"


def _html(name):
    return (WEB / name).read_text(encoding="utf-8")


def test_r443_chat_layout_slides_with_a_margin_and_hides_after():
    h = _html("index_v2.html")
    assert "body.sb-closed #side{display:none}" not in h
    assert "#side{margin-left:0;transition:margin-left .22s ease,visibility 0s}" in h
    assert "body.sb-closed #side{margin-left:calc(-1*var(--side) - 1px);visibility:hidden;transition:margin-left .22s ease,visibility 0s .22s}" in h
    assert "body.sb-closed #side{margin-left:calc(-1*min(300px,85vw) - 40px)}" in h     # phone overlay
    assert "scrim-in" in h and "transition:none!important" in h                       # scrim fades, reduced motion stops it


def test_r443_paper_drawer_slides_from_the_right():
    h = _html("index_paper.html")
    assert "body.sb-closed aside{display:none}" not in h
    assert "body.sb-closed aside{transform:translateX(105%);visibility:hidden;transition:transform .25s ease,visibility 0s .25s}" in h
    assert "prefers-reduced-motion:reduce){#scrim{animation:none}aside" in h


def test_r443_classic_layout_slides_with_a_margin():
    h = _html("index.html")
    assert "body.sb-closed aside{display:none}" not in h
    assert "body.sb-closed aside{margin-left:-241px;visibility:hidden;" in h and "prefers-reduced-motion:reduce){aside" in h
