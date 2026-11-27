"""R457: edit_file applies an edit whose `old` differs from the file only in
whitespace, when that match is unique; the approval preview shows the same."""
from aurora import approve, tools
from aurora.patch import whitespace_edit

SRC = "def f():\n    if x:\n        return 1\n    return 2\n"


def test_reindents_new_to_the_files_indentation(tmp_path):
    p = tmp_path / "a.py"
    p.write_text(SRC)
    out = tools.edit_file(str(p), "if x:\n    return 1", "if x:\n    log()\n    return 1")
    assert "re-indented" in out
    assert p.read_text() == "def f():\n    if x:\n        log()\n        return 1\n    return 2\n"


def test_tabs_vs_spaces_and_inner_spacing(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n\tval  =  compute( a )\ny = 2\n")
    out = tools.edit_file(str(p), "    val = compute( a )", "    val = compute(b)")
    assert out.startswith("[edited")
    assert p.read_text() == "x = 1\n\tval = compute(b)\ny = 2\n"


def test_ambiguous_whitespace_match_is_still_an_error(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("  a = 1\n    a = 1\n")
    out = tools.edit_file(str(p), "a  =  1", "a = 2")
    assert out.startswith("[error") and p.read_text() == "  a = 1\n    a = 1\n"


def test_a_real_difference_is_still_an_error(tmp_path):
    p = tmp_path / "a.py"
    p.write_text(SRC)
    assert tools.edit_file(str(p), "if y:\n    return 1", "z").startswith("[error")
    assert p.read_text() == SRC


def test_exact_match_path_is_unchanged(tmp_path):
    p = tmp_path / "a.py"
    p.write_text(SRC)
    assert tools.edit_file(str(p), "return 2", "return 3") == f"[edited {p}]"


def test_part_of_a_line_never_fuzzy_matches():
    assert whitespace_edit("total_count = 5\n", "count  = 5", "count = 6") is None


def test_preview_shows_the_fuzzy_edit(tmp_path):
    p = tmp_path / "a.py"
    p.write_text(SRC)
    prev = approve._diff_preview("edit_file", {"path": str(p), "old": "if x:\n    return 1",
                                               "new": "if x:\n    return 9"})
    assert "+        return 9" in prev and "edit will fail" not in prev
