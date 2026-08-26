"""Tests for aurora.mdrender's non-fence rendering paths (headers, bullets,
inline bold/code, linkify integration) and fence edge cases — the existing
coverage in test_core.py/test_finish.py focuses on in-fence syntax
highlighting; these paths were untested."""

from aurora import mdrender


def _colours_on(monkeypatch):
    for name, seq in (("RESET", "\033[0m"), ("DIM", "\033[2m"),
                      ("BOLD", "\033[1m"), ("CYAN", "\033[36m"),
                      ("GREEN", "\033[32m"), ("YELLOW", "\033[33m"),
                      ("MAGENTA", "\033[35m")):
        monkeypatch.setattr(mdrender, name, seq)


def test_header_line_is_bold_cyan_hash_marks_stripped(monkeypatch):
    """Matches ui.py's actual calling convention: a completed line with its
    trailing "\\n" already split off (the caller re-appends "\\n" itself)."""
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    out = r.render("## Section Title")
    assert out == "\033[1m\033[36mSection Title\033[0m"


def test_header_levels_one_through_six_all_match(monkeypatch):
    _colours_on(monkeypatch)
    for level in range(1, 7):
        r = mdrender.LineRenderer()
        out = r.render(f"{'#' * level} Title")
        assert out == "\033[1m\033[36mTitle\033[0m"


def test_header_line_preserves_a_trailing_newline_if_present(monkeypatch):
    """Every other branch (bold/code/bullet via .sub, the fence branches via
    plain concatenation) preserves whatever trailing content was in `line`
    — the header branch used to silently drop a trailing "\\n" because
    `(.*)$` doesn't consume it. A caller other than ui.py's stripped-line
    convention must not lose it."""
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    out = r.render("## Section Title\n")
    assert out == "\033[1m\033[36mSection Title\033[0m\n"


def test_seven_hashes_is_not_a_header(monkeypatch):
    """The regex caps at 6 #'s (valid markdown headers go up to h6) —
    7 must fall through to ordinary line rendering instead."""
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    out = r.render("####### not a header\n")
    assert "\033[1m\033[36m" not in out


def test_bullet_dash_becomes_bullet_point(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    out = r.render("- first item\n")
    assert out.startswith("• first item")


def test_bullet_star_becomes_bullet_point(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    out = r.render("* first item\n")
    assert out.startswith("• first item")


def test_nested_bullet_preserves_leading_indentation(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    out = r.render("  - nested item\n")
    assert out.startswith("  • nested item")


def test_bold_inline_marker_wraps_in_bold_and_reset(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    out = r.render("this is **important** text\n")
    assert out == "this is \033[1mimportant\033[0m text\n"


def test_inline_code_marker_wraps_in_cyan_and_reset(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    out = r.render("run `ls -la` now\n")
    assert out == "run \033[36mls -la\033[0m now\n"


def test_bold_and_inline_code_compose_on_the_same_line(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    out = r.render("**note**: run `ls`\n")
    assert "\033[1mnote\033[0m" in out
    assert "\033[36mls\033[0m" in out


def test_plain_prose_with_no_markdown_markers_is_linkified_only(monkeypatch):
    _colours_on(monkeypatch)
    monkeypatch.setattr(mdrender, "linkify", lambda s: s.upper())
    r = mdrender.LineRenderer()
    out = r.render("just prose\n")
    assert out == "JUST PROSE\n"


def test_headers_are_not_passed_through_linkify(monkeypatch):
    """A header line returns early before the bold/code/bullet/linkify
    chain — a URL in a header title is never wrapped."""
    _colours_on(monkeypatch)
    called = []
    monkeypatch.setattr(mdrender, "linkify",
                        lambda s: called.append(s) or s)
    r = mdrender.LineRenderer()
    r.render("# See http://example.com\n")
    assert called == []


# ── fence state machine ───────────────────────────────────────────────────

def test_fence_close_without_matching_open_still_toggles(monkeypatch):
    """A stray closing fence with no prior open is symmetric — it flips
    in_fence back off from an already-off state, i.e. ON. Documents the
    actual (simple, line-based) behavior rather than assuming markdown
    validity."""
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    assert r.in_fence is False
    r.render("```\n")
    assert r.in_fence is True


def test_unclosed_fence_at_end_of_stream_leaves_state_open(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    r.render("```python\n")
    r.render("x = 1\n")
    assert r.in_fence is True
    assert r.fence_lang == "python"


def test_fence_lang_cleared_on_close(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    r.render("```python\n")
    r.render("```\n")
    assert r.in_fence is False
    assert r.fence_lang is None


def test_indented_fence_marker_still_detected(monkeypatch):
    """`line.strip().startswith('```')` — a fence marker indented under a
    bullet/nested list item must still toggle fence state."""
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    r.render("  ```python\n")
    assert r.in_fence is True
    assert r.fence_lang == "python"


def test_new_line_renderer_instance_starts_with_clean_state(monkeypatch):
    """Each turn gets its own LineRenderer (per the class docstring) — a
    fresh instance must never inherit another turn's dangling open fence."""
    _colours_on(monkeypatch)
    r1 = mdrender.LineRenderer()
    r1.render("```python\n")
    r2 = mdrender.LineRenderer()
    assert r2.in_fence is False
    assert r2.fence_lang is None


# ── _resolve_lang ─────────────────────────────────────────────────────────

def test_resolve_lang_is_case_insensitive():
    assert mdrender._resolve_lang("PYTHON") == "python"
    assert mdrender._resolve_lang("Py") == "python"


def test_resolve_lang_strips_whitespace():
    assert mdrender._resolve_lang("  python  ") == "python"


def test_resolve_lang_unknown_returns_none():
    assert mdrender._resolve_lang("prolog") is None


def test_resolve_lang_empty_returns_none():
    assert mdrender._resolve_lang("") is None


# ── per-language keyword highlighting (only python/js/ruby covered above) ──

def test_go_keyword_highlighted(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    r.render("```go\n")
    out = r.render("func main() {\n")
    assert "\033[35mfunc\033[0m" in out


def test_rust_keyword_highlighted(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    r.render("```rust\n")
    out = r.render("let mut x = 1;\n")
    assert "\033[35mlet\033[0m" in out
    assert "\033[35mmut\033[0m" in out


def test_bash_comment_and_keyword(monkeypatch):
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    r.render("```bash\n")
    out = r.render('if [ -f x ]; then echo "hi"; fi  # done\n')
    assert "\033[35mif\033[0m" in out
    assert "\033[2m# done" in out


def test_double_slash_comment_language_uses_double_slash_not_hash(monkeypatch):
    """javascript/go/rust use `//` comments, not `#` — the LINE_COMMENT
    table must actually be consulted, not hardcoded to '#'."""
    _colours_on(monkeypatch)
    r = mdrender.LineRenderer()
    r.render("```javascript\n")
    out = r.render('const x = 1; // trailing comment\n')
    assert "\033[2m// trailing comment" in out
    # a bare '#' in JS source (not a comment marker there) must NOT be
    # treated as starting a comment
    r2 = mdrender.LineRenderer()
    r2.render("```javascript\n")
    out2 = r2.render("const x = a#b;\n")
    assert "\033[2m#b" not in out2
