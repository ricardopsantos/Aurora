"""colors.linkify / URL_RE (P-6: control-char escape injection via URLs)."""

from aurora import colors


def test_url_re_excludes_escape_byte():
    """A URL containing a raw ESC (0x1b) must not match — \\s alone doesn't
    exclude it, so a "URL" like `http://a.com/\\x1b]52;c;PAYLOAD\\x07` used to
    match whole, and linkify() would splice it verbatim into an OSC-8
    hyperlink escape, letting fetched content smuggle an arbitrary terminal
    escape sequence into what the terminal actually interprets."""
    text = "see http://a.com/\x1b]52;c;PAYLOAD\x07 for details"
    m = colors.URL_RE.search(text)
    assert m is not None
    assert "\x1b" not in m.group(0)


def test_linkify_never_embeds_a_control_byte_in_its_output(monkeypatch):
    monkeypatch.setattr(colors, "RESET", "\033[0m")  # force "enabled"
    monkeypatch.setattr(colors, "IN_TUI", False)
    text = "http://a.com/\x1b]52;c;PAYLOAD\x07end"
    out = colors.linkify(text)
    # the OSC-8 wrapper itself legitimately contains \x1b — only the URL
    # SPLICED INTO it must be clean
    import re
    for m in re.finditer(r"\x1b\]8;;(.*?)\x1b\\", out):
        assert "\x1b" not in m.group(1)
