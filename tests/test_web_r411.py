"""R411: a tap outside the Uploads or Notes frame closes it; New Folder sits on the Chats header."""
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "aurora/web"


def test_r411_backdrop_closes_uploads_and_notes():
    js = (WEB / "app.js").read_text()
    # R418: on the chat page they fill the chat area instead, so only a real dialog closes on a backdrop tap
    assert "if (e.target === dlg && !dlg.classList.contains('panel')) closeNotes();" in js
    assert "if (e.target === dlg && !dlg.classList.contains('panel')) dlg.close();" in js


def test_r411_new_folder_is_in_the_chats_header():
    html = (WEB / "index_v2.html").read_text()
    head = html[html.index('<div class="chats-head">'):]
    head = head[:head.index("</div>")]
    assert 'id="chats-cap"' in head and 'id="new-folder"' in head and "New Folder</button>" not in html
