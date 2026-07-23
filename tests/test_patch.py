"""Unit tests for aurora/patch.py (R97) — pure functions, no I/O."""

import pytest

from aurora import patch


def test_apply_single_hunk_changes_one_line():
    text = "one\ntwo\nthree\n"
    diff = ("@@ -1,3 +1,3 @@\n"
            " one\n"
            "-two\n"
            "+TWO\n"
            " three\n")
    hunks = patch.parse(diff)
    assert patch.apply(text, hunks) == "one\nTWO\nthree\n"


def test_apply_multiple_hunks_in_order():
    """Later hunks apply against the RESULT of earlier ones, not the
    original text — this is what makes them independent of each other's
    line-number drift."""
    text = "a\nb\nc\nd\ne\n"
    diff = ("@@ -1,2 +1,2 @@\n"
            " a\n"
            "-b\n"
            "+B\n"
            "@@ -4,2 +4,2 @@\n"
            " d\n"
            "-e\n"
            "+E\n")
    hunks = patch.parse(diff)
    assert patch.apply(text, hunks) == "a\nB\nc\nd\nE\n"


def test_ignores_file_header_lines():
    """--- / +++ lines are read and discarded — the CALLER's path argument
    is the only authority on which file gets written, never the diff text."""
    text = "x\ny\n"
    diff = ("--- a/some/other/path.py\n"
            "+++ b/some/other/path.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-x\n"
            "+X\n"
            " y\n")
    hunks = patch.parse(diff)
    assert patch.apply(text, hunks) == "X\ny\n"


def test_no_newline_at_end_of_file_marker_is_skipped():
    text = "one\ntwo"
    diff = ("@@ -1,2 +1,2 @@\n"
            " one\n"
            "-two\n"
            "\\ No newline at end of file\n"
            "+TWO\n"
            "\\ No newline at end of file\n")
    hunks = patch.parse(diff)
    assert patch.apply(text, hunks) == "one\nTWO"


def test_blank_line_in_hunk_is_treated_as_empty_context_line():
    """Models often forget the leading space marker on a blank context
    line — treat a bare blank line inside a hunk as context, not an error."""
    text = "a\n\nb\n"
    diff = ("@@ -1,3 +1,3 @@\n"
            " a\n"
            "\n"
            "-b\n"
            "+B\n")
    hunks = patch.parse(diff)
    assert patch.apply(text, hunks) == "a\n\nB\n"


def test_context_only_hunk_is_a_no_op_not_an_error():
    text = "same\n"
    diff = "@@ -1,1 +1,1 @@\n same\n"
    hunks = patch.parse(diff)
    assert patch.apply(text, hunks) == text


def test_context_not_found_raises():
    text = "one\ntwo\nthree\n"
    diff = "@@ -1,1 +1,1 @@\n-nonexistent\n+replacement\n"
    hunks = patch.parse(diff)
    with pytest.raises(patch.PatchError, match="context not found"):
        patch.apply(text, hunks)


def test_ambiguous_context_raises():
    text = "dup\ndup\ndup\n"
    diff = "@@ -1,1 +1,1 @@\n-dup\n+DUP\n"
    hunks = patch.parse(diff)
    with pytest.raises(patch.PatchError, match="matches 3 times"):
        patch.apply(text, hunks)


def test_all_or_nothing_first_bad_hunk_stops_before_later_ones_matter():
    """apply() itself doesn't write anything — this just confirms it raises
    on the FIRST bad hunk rather than silently skipping to the next one,
    which is what makes atomicity at the caller (tools.apply_patch) safe:
    the caller never sees a partially-applied result to accidentally write."""
    text = "a\nb\n"
    diff = ("@@ -1,1 +1,1 @@\n-a\n+A\n"
            "@@ -1,1 +1,1 @@\n-nonexistent\n+X\n")
    hunks = patch.parse(diff)
    with pytest.raises(patch.PatchError, match="context not found"):
        patch.apply(text, hunks)


def test_pure_insertion_with_no_anchor_is_rejected_at_parse_time():
    """A hunk with only '+' lines has nothing to search for — text.count("")
    would match everywhere, so this must be a clear parse-time error, not a
    silent misapplication at the start of the file."""
    diff = "@@ -0,0 +1,1 @@\n+new line\n"
    with pytest.raises(patch.PatchError, match="no surrounding context"):
        patch.parse(diff)


def test_empty_hunk_is_rejected():
    diff = "@@ -1,0 +1,0 @@\n"
    with pytest.raises(patch.PatchError, match="empty"):
        patch.parse(diff)


def test_no_hunks_at_all_is_rejected():
    with pytest.raises(patch.PatchError, match="no hunks found"):
        patch.parse("this is not a diff\njust some text\n")


def test_bad_line_prefix_is_rejected():
    diff = "@@ -1,1 +1,1 @@\n*garbage line\n"
    with pytest.raises(patch.PatchError, match="bad line"):
        patch.parse(diff)
