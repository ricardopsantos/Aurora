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


def _force_enabled(monkeypatch):
    monkeypatch.setattr(colors, "RESET", "\033[0m")
    monkeypatch.setattr(colors, "CYAN", "\033[36m")
    monkeypatch.setattr(colors, "UNDERLINE", "\033[4m")
    monkeypatch.setattr(colors, "IN_TUI", False)


def test_linkify_wraps_a_plain_url_in_osc8(monkeypatch):
    _force_enabled(monkeypatch)
    out = colors.linkify("see http://example.com/page for details")
    assert "\x1b]8;;http://example.com/page\x1b\\" in out
    assert out.count("http://example.com/page") == 2  # OSC-8 target + visible text


def test_linkify_excludes_trailing_sentence_punctuation(monkeypatch):
    _force_enabled(monkeypatch)
    out = colors.linkify("visit http://example.com/page.")
    assert "\x1b]8;;http://example.com/page\x1b\\" in out
    assert "\x1b]8;;http://example.com/page.\x1b\\" not in out
    assert out.endswith(".")


def test_linkify_excludes_trailing_closing_paren(monkeypatch):
    _force_enabled(monkeypatch)
    out = colors.linkify("(see http://example.com/page)")
    assert "\x1b]8;;http://example.com/page\x1b\\" in out
    assert out.endswith(")")


def test_linkify_is_a_noop_when_colours_disabled(monkeypatch):
    monkeypatch.setattr(colors, "RESET", "")   # "disabled" per _enabled()
    monkeypatch.setattr(colors, "IN_TUI", False)
    text = "see http://example.com/page for details"
    assert colors.linkify(text) == text


def test_linkify_is_a_noop_inside_the_tui(monkeypatch):
    """The full-screen TUI re-parses stdout with an ANSI parser that only
    understands CSI, not OSC-8 — linkify() must not touch anything while
    IN_TUI is set, even with colours otherwise enabled."""
    monkeypatch.setattr(colors, "RESET", "\033[0m")
    monkeypatch.setattr(colors, "IN_TUI", True)
    text = "see http://example.com/page for details"
    assert colors.linkify(text) == text


def test_linkify_handles_multiple_urls_independently(monkeypatch):
    _force_enabled(monkeypatch)
    out = colors.linkify("first http://a.com then http://b.com/x")
    assert "\x1b]8;;http://a.com\x1b\\" in out
    assert "\x1b]8;;http://b.com/x\x1b\\" in out


def test_linkify_no_urls_returns_text_unchanged(monkeypatch):
    _force_enabled(monkeypatch)
    text = "no links here at all"
    assert colors.linkify(text) == text


def test_dim_wraps_with_dim_and_reset(monkeypatch):
    monkeypatch.setattr(colors, "DIM", "\033[2m")
    monkeypatch.setattr(colors, "RESET", "\033[0m")
    assert colors.dim("hello") == "\033[2mhello\033[0m"


def test_bold_wraps_with_bold_and_reset(monkeypatch):
    monkeypatch.setattr(colors, "BOLD", "\033[1m")
    monkeypatch.setattr(colors, "RESET", "\033[0m")
    assert colors.bold("hello") == "\033[1mhello\033[0m"


def test_dim_and_bold_are_plain_when_colours_disabled(monkeypatch):
    monkeypatch.setattr(colors, "DIM", "")
    monkeypatch.setattr(colors, "BOLD", "")
    monkeypatch.setattr(colors, "RESET", "")
    assert colors.dim("hello") == "hello"
    assert colors.bold("hello") == "hello"


def test_colour_diff_marks_additions_removals_and_hunk_headers(monkeypatch):
    monkeypatch.setattr(colors, "GREEN", "\033[32m")
    monkeypatch.setattr(colors, "RED", "\033[31m")
    monkeypatch.setattr(colors, "CYAN", "\033[36m")
    monkeypatch.setattr(colors, "DIM", "\033[2m")
    monkeypatch.setattr(colors, "RESET", "\033[0m")
    diff = "@@ -1,2 +1,2 @@\n-old line\n+new line\n context line"
    out = colors.colour_diff(diff)
    lines = out.splitlines()
    assert lines[0] == "\033[36m@@ -1,2 +1,2 @@\033[0m"
    assert lines[1] == "\033[31m-old line\033[0m"
    assert lines[2] == "\033[32m+new line\033[0m"
    assert lines[3] == "\033[2m context line\033[0m"


def test_colour_diff_file_header_lines_are_not_treated_as_additions_removals(monkeypatch):
    """+++ / --- file-header lines must fall through to the plain/dim
    branch, not be miscoloured as a single-line addition/removal."""
    monkeypatch.setattr(colors, "GREEN", "\033[32m")
    monkeypatch.setattr(colors, "RED", "\033[31m")
    monkeypatch.setattr(colors, "DIM", "\033[2m")
    monkeypatch.setattr(colors, "RESET", "\033[0m")
    diff = "--- a/file.py\n+++ b/file.py"
    out = colors.colour_diff(diff)
    lines = out.splitlines()
    assert lines[0] == "\033[2m--- a/file.py\033[0m"
    assert lines[1] == "\033[2m+++ b/file.py\033[0m"


def test_colour_diff_empty_input():
    assert colors.colour_diff("") == ""


def test_strip_dangerous_escapes_available_from_colors_module():
    """man/tui import strip_dangerous_escapes FROM colors — a regression
    here would silently break the re-export other modules rely on."""
    payload = "before\x1b]52;c;cGF5bG9hZA==\x07after"
    assert colors.strip_dangerous_escapes(payload) == "beforeafter"
