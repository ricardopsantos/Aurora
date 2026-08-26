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


# R220: isolated blank-line removal used to silently no-op — old_lines=[""]
# and new_lines=[] both join to "" via "\n".join(...), so a naive
# `h.old == h.new` check treated a real deletion as a context-only hunk.
def test_removing_an_isolated_blank_line_is_not_silently_a_no_op():
    text = "x\n\ny\n"
    diff = "@@ -2,1 +2,0 @@\n-\n"
    hunks = patch.parse(diff)
    assert hunks[0].changed is True
    # "" as an anchor is inherently ambiguous — must raise, not silently
    # leave the file unchanged.
    with pytest.raises(patch.PatchError, match="matches .* times"):
        patch.apply(text, hunks)


def test_inserting_an_isolated_blank_line_is_not_silently_a_no_op():
    """Mirror case: a pure-addition of a blank line has old_lines=[] and
    new_lines=[""] — also collides to "" == "" under naive string equality,
    but is rejected earlier as a pure insertion with no anchor."""
    diff = "@@ -0,0 +1,1 @@\n+\n"
    with pytest.raises(patch.PatchError, match="no surrounding context"):
        patch.parse(diff)


def test_context_only_hunk_still_marked_unchanged():
    """Sanity check the fix didn't break the real no-op case: a hunk that is
    ENTIRELY context (old_lines == new_lines, non-degenerate) must still be
    a silent skip, not an error."""
    text = "same\n"
    diff = "@@ -1,1 +1,1 @@\n same\n"
    hunks = patch.parse(diff)
    assert hunks[0].changed is False
    assert patch.apply(text, hunks) == text


def test_context_only_blank_line_hunk_is_still_a_no_op():
    """A hunk of ONE blank context line (old_lines == new_lines == [""]) is
    genuinely unchanged — must stay a no-op, distinct from the removal case
    above where the lists differ in length."""
    text = "a\n\nb\n"
    diff = "@@ -2,1 +2,1 @@\n \n"
    hunks = patch.parse(diff)
    assert hunks[0].changed is False
    assert patch.apply(text, hunks) == text


def test_multi_hunk_patch_with_real_and_noop_hunks():
    text = "a\nb\nc\n"
    diff = ("@@ -1,1 +1,1 @@\n a\n"
            "@@ -2,1 +2,1 @@\n-b\n+B\n")
    hunks = patch.parse(diff)
    assert hunks[0].changed is False
    assert hunks[1].changed is True
    assert patch.apply(text, hunks) == "a\nB\nc\n"


def test_apply_does_not_mutate_input_text_on_error():
    """apply() must not have any observable side effect on failure — the
    caller relies on all-or-nothing to decide whether to write to disk."""
    text = "one\ntwo\nthree\n"
    diff = "@@ -1,1 +1,1 @@\n-nonexistent\n+replacement\n"
    hunks = patch.parse(diff)
    original = text
    with pytest.raises(patch.PatchError):
        patch.apply(text, hunks)
    assert text == original


def test_trailing_backslash_no_newline_marker_without_preceding_hunk_line():
    """A stray '\\ No newline...' marker with nothing else in the hunk must
    not be silently treated as an empty hunk without raising."""
    diff = "@@ -1,1 +1,1 @@\n-x\n\\ No newline at end of file\n+X\n"
    hunks = patch.parse(diff)
    assert patch.apply("x", hunks) == "X"


def test_multiple_separate_hunks_targeting_disjoint_regions():
    text = "1\n2\n3\n4\n5\n6\n7\n"
    diff = ("@@ -1,1 +1,1 @@\n-1\n+ONE\n"
            "@@ -4,1 +4,1 @@\n-4\n+FOUR\n"
            "@@ -7,1 +7,1 @@\n-7\n+SEVEN\n")
    hunks = patch.parse(diff)
    assert patch.apply(text, hunks) == "ONE\n2\n3\nFOUR\n5\n6\nSEVEN\n"


def test_hunk_removing_multiple_lines_at_once():
    text = "keep1\nremove1\nremove2\nkeep2\n"
    diff = ("@@ -1,4 +1,2 @@\n keep1\n-remove1\n-remove2\n keep2\n")
    hunks = patch.parse(diff)
    assert patch.apply(text, hunks) == "keep1\nkeep2\n"


def test_hunk_with_only_additions_and_trailing_context_still_requires_anchor():
    """Additions at the very end with no leading context are still a pure
    insertion (old_lines empty) and must be rejected."""
    diff = "@@ -3,0 +4,2 @@\n+new1\n+new2\n"
    with pytest.raises(patch.PatchError, match="no surrounding context"):
        patch.parse(diff)


def test_second_hunk_ambiguous_after_first_hunk_creates_duplicate():
    """A hunk can become ambiguous only because an EARLIER hunk in the same
    patch introduced a duplicate — apply() must catch this at the point it
    actually occurs, not just when the original text was already ambiguous."""
    text = "a\nb\n"
    diff = ("@@ -1,1 +1,1 @@\n-a\n+b\n"      # now text is "b\nb\n"
            "@@ -2,1 +2,1 @@\n-b\n+B\n")     # "b" now matches twice
    hunks = patch.parse(diff)
    with pytest.raises(patch.PatchError, match="matches 2 times"):
        patch.apply(text, hunks)


# ── R236: '---'/'+++' in hunk CONTENT is not a file header ───────────────

def test_removing_a_yaml_front_matter_delimiter_is_not_dropped():
    """R236: `----` (the removal of a `---` line) used to satisfy
    startswith("---"), truncating the hunk body there and silently
    no-op-ing the deletion while reporting success."""
    hunks = patch.parse("@@ -1,4 +1,3 @@\n title: x\n----\n body\n tail\n")
    out = patch.apply("title: x\n---\nbody\ntail\n", hunks)
    assert out == "title: x\nbody\ntail\n"


def test_a_hunk_is_not_truncated_at_a_removed_rule_leaving_a_partial_apply():
    """R236: the worse shape — the edit before the `---` applied and the
    deletion after it vanished, breaking the all-or-nothing contract."""
    hunks = patch.parse("@@ -1,5 +1,4 @@\n a\n-old\n+new\n----\n tail\n")
    assert patch.apply("a\nold\n---\ntail\n", hunks) == "a\nnew\ntail\n"


def test_adding_a_line_of_plus_signs_is_not_treated_as_a_file_header():
    hunks = patch.parse("@@ -1,2 +1,3 @@\n a\n+++\n tail\n")
    assert patch.apply("a\ntail\n", hunks) == "a\n++\ntail\n"


def test_removing_a_double_dash_line_is_not_treated_as_a_file_header():
    hunks = patch.parse("@@ -1,3 +1,2 @@\n a\n---\n tail\n")
    assert patch.apply("a\n--\ntail\n", hunks) == "a\ntail\n"


def test_real_file_headers_still_end_a_hunk_body():
    """The pair `--- path` / `+++ path` (marker then a space) is still
    recognised, so a multi-file `diff -u` parses into separate hunks."""
    diff = ("--- a/one\n+++ b/one\n@@ -1,2 +1,2 @@\n a\n-x\n+y\n"
            "--- a/two\n+++ b/two\n@@ -1,2 +1,2 @@\n b\n-p\n+q\n")
    hunks = patch.parse(diff)
    assert len(hunks) == 2
    assert hunks[0].old == "a\nx" and hunks[1].old == "b\np"


def test_a_git_diff_section_line_ends_a_hunk_body():
    diff = ("@@ -1,2 +1,2 @@\n a\n-x\n+y\n"
            "diff --git a/two b/two\nindex 1..2 100644\n"
            "--- a/two\n+++ b/two\n@@ -1,2 +1,2 @@\n b\n-p\n+q\n")
    hunks = patch.parse(diff)
    assert len(hunks) == 2
    assert hunks[0].new == "a\ny" and hunks[1].new == "b\nq"
