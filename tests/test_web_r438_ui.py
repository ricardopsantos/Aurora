"""R438: a table right under a line of text (no blank line) is a table; an indented '# x' does not hang the page."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser  # noqa: E402,F401  (fixture)

REPLY = ("Here's the metadata:\n\n**Audio details:**\n| Property | Value |\n|----------|-------|\n| Codec | MP3 |\n| Duration | 4:58 |\n\n"
         "**Tags:**\n| Tag | Value |\n|-----|-------|\n| Title | Thunderstruck |\n\nText then a rule\n---\nafter\n\n  # indented\nend")


def test_r438_table_under_a_text_line(browser, app):
    page = browser.new_page(); page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    out = page.evaluate("""r => { const d = document.createElement('div'); mdRender(d, r);
        return { tables: [...d.querySelectorAll('table')].map(t => [...t.querySelectorAll('tr')].map(tr => [...tr.children].map(c => c.textContent))),
                 ps: [...d.querySelectorAll('p')].map(p => p.textContent), hr: d.querySelectorAll('hr').length,
                 h: [...d.querySelectorAll('h1')].map(h => h.textContent) }; }""", REPLY)
    assert out["tables"] == [[["Property", "Value"], ["Codec", "MP3"], ["Duration", "4:58"]], [["Tag", "Value"], ["Title", "Thunderstruck"]]]
    assert "Audio details:" in out["ps"] and "Tags:" in out["ps"] and not any("|" in p for p in out["ps"])
    assert out["hr"] == 1 and out["h"] == ["indented"] and "end" in out["ps"][-1]   # R439: up to 3 spaces still make a heading (CommonMark)
    page.close()
