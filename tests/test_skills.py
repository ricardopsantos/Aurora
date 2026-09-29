"""Additional tests for aurora.skills — dir_stamp() (previously untested),
config_base vs AURORA_HOME shadowing, executable-bit dispatch, and the
discover() TOCTOU/permission-race fix (an unreadable skills dir used to
crash discovery entirely instead of degrading gracefully, unlike
dir_stamp()'s already-existing same-shaped guard)."""

import os
import stat

from aurora import skills


def _mk_skill(d, name, body="print('ran')\n", executable=False):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body)
    if executable:
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


# ── discover(): permission/TOCTOU robustness ────────────────────────────

def test_discover_survives_an_unreadable_skills_directory(tmp_path, monkeypatch):
    """Regression: a skills dir that is a real directory (passes _dirs()'s
    is_dir() check) but cannot be listed used to raise an uncaught OSError
    out of discover() entirely — reachable behind the /command completer,
    once per keystroke. Must degrade to skipping that dir, not crash."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    home_skills = tmp_path / "home" / "skills"
    _mk_skill(home_skills, "hello.py")
    os.chmod(home_skills, 0o000)
    try:
        result = skills.discover()
        assert result == {}   # unreadable dir contributes nothing, no crash
    finally:
        os.chmod(home_skills, 0o755)


def test_discover_other_dirs_still_work_when_one_is_unreadable(tmp_path, monkeypatch):
    """A broken AURORA_HOME/skills must not hide a working config_base
    skills dir — each directory in _dirs() is independent."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    home_skills = tmp_path / "home" / "skills"
    home_skills.mkdir(parents=True)
    os.chmod(home_skills, 0o000)
    config_skills = tmp_path / "project" / "skills"
    _mk_skill(config_skills, "works.py")
    try:
        result = skills.discover(str(tmp_path / "project"))
        assert "works" in result
    finally:
        os.chmod(home_skills, 0o755)


# ── dir_stamp() ──────────────────────────────────────────────────────────

def test_dir_stamp_changes_when_a_skill_is_added(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    home_skills = tmp_path / "home" / "skills"
    home_skills.mkdir(parents=True)
    before = skills.dir_stamp()
    import time
    time.sleep(0.01)
    _mk_skill(home_skills, "new.py")
    after = skills.dir_stamp()
    assert before != after


def test_dir_stamp_stable_when_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    home_skills = tmp_path / "home" / "skills"
    _mk_skill(home_skills, "a.py")
    s1 = skills.dir_stamp()
    s2 = skills.dir_stamp()
    assert s1 == s2


def test_dir_stamp_empty_when_no_skills_dirs_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "nothing-here"))
    assert skills.dir_stamp() == ()


def test_dir_stamp_survives_a_dir_disappearing_mid_call(tmp_path, monkeypatch):
    """R96a's own documented race: _dirs() confirms is_dir(), then the dir
    vanishes before stat() runs. Must skip it, not raise."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    home_skills = tmp_path / "home" / "skills"
    home_skills.mkdir(parents=True)

    import aurora.skills as skillsmod
    real_dirs = skillsmod._dirs

    def dirs_then_remove(config_base):
        d = real_dirs(config_base)
        home_skills.rmdir()   # gone by the time dir_stamp's stat() runs
        return d

    monkeypatch.setattr(skillsmod, "_dirs", dirs_then_remove)
    assert skillsmod.dir_stamp() == ()


# ── config_base vs AURORA_HOME shadowing ────────────────────────────────

def test_config_base_skills_shadow_aurora_home_skills_with_same_name(
        tmp_path, monkeypatch):
    """'earlier dirs shadow later ones' — config_base/skills is listed
    first in _dirs(), so a same-named skill there wins."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "dup.py", "print('home')\n")
    _mk_skill(tmp_path / "project" / "skills", "dup.py", "print('project')\n")
    found = skills.discover(str(tmp_path / "project"))
    assert found["dup"] == tmp_path / "project" / "skills" / "dup.py"


def test_distinct_names_from_both_dirs_both_appear(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "from_home.py")
    _mk_skill(tmp_path / "project" / "skills", "from_project.py")
    found = skills.discover(str(tmp_path / "project"))
    assert "from_home" in found
    assert "from_project" in found


def test_no_config_base_only_aurora_home_is_searched(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "only.py")
    found = skills.discover(None)
    assert "only" in found


# ── run(): executable-bit dispatch, args parsing ────────────────────────

def test_run_non_executable_py_uses_venv_python(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "plain.py",
             "print('via interpreter')\n", executable=False)
    out = skills.run("plain", "", None)
    assert "via interpreter" in out


def test_run_executable_py_uses_its_own_shebang(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    import sys
    body = f"#!{sys.executable}\nprint('via shebang')\n"
    _mk_skill(tmp_path / "home" / "skills", "exec.py", body, executable=True)
    out = skills.run("exec", "", None)
    assert "via shebang" in out


def test_run_unbalanced_quotes_in_args_reports_error_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "s.py", "print('x')\n")
    out = skills.run("s", 'unterminated "quote', None)
    assert "skill args error" in out


def test_run_unknown_skill_reports_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "skills").mkdir(parents=True)
    out = skills.run("nonexistent", "", None)
    assert "unknown skill: /nonexistent" in out
    assert "/skills" in out


def test_run_exit_code_appended_on_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "fail.py",
             "import sys; sys.exit(3)\n")
    out = skills.run("fail", "", None)
    assert "[exit 3]" in out


def test_run_no_output_reports_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "silent.py", "pass\n")
    out = skills.run("silent", "", None)
    assert "[no output]" in out


def test_run_zero_exit_does_not_append_exit_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "ok.py", "print('done')\n")
    out = skills.run("ok", "", None)
    assert "[exit" not in out


# ── listing() ────────────────────────────────────────────────────────────

def test_listing_empty_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    assert "no skills installed" in skills.listing(None)


def test_listing_includes_blurb(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "greet.py",
             "# greets the user\nprint('hi')\n")
    out = skills.listing(None)
    assert "/greet" in out
    assert "greets the user" in out


# ── doc skills (R247) ─────────────────────────────────────────────────────
# A second skill kind: a Markdown SKILL.md (frontmatter + prose), the same
# shape Claude Code and Hermes both use. discover_docs()/load_doc() only run
# against directories the CALLER names via doc_roots — no path on this
# machine or any other is ever assumed.

def _mk_doc_skill(root, name, description="does a thing", body="Do the thing.\n"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}")
    return d / "SKILL.md"


def test_doc_roots_empty_when_unconfigured(tmp_path):
    # No doc_roots argument at all — must not fall back to any path.
    assert skills.discover_docs(None) == {}
    assert skills.discover_docs([]) == {}


def test_doc_roots_ignores_a_nonexistent_path(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    assert skills.discover_docs([missing]) == {}


def test_discover_docs_finds_a_nested_skill_md(tmp_path):
    root = tmp_path / "library"
    _mk_doc_skill(root / "category", "my-skill")
    found = skills.discover_docs([str(root)])
    assert set(found) == {"my-skill"}
    assert found["my-skill"].name == "SKILL.md"


def test_discover_docs_earlier_root_shadows_later(tmp_path):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    _mk_doc_skill(r1, "dup", description="from r1")
    _mk_doc_skill(r2, "dup", description="from r2")
    found = skills.discover_docs([str(r1), str(r2)])
    assert "from r1" in found["dup"].read_text()


def test_discover_docs_survives_an_unreadable_root(tmp_path):
    root = tmp_path / "library"
    _mk_doc_skill(root, "ok-skill")
    os.chmod(root, 0o000)
    try:
        # Not asserting emptiness here — root itself is unreadable so
        # rglob raises, caught the same way discover() tolerates it.
        result = skills.discover_docs([str(root)])
        assert result == {}
    finally:
        os.chmod(root, 0o755)


def test_load_doc_strips_frontmatter_and_keeps_body(tmp_path):
    root = tmp_path / "library"
    _mk_doc_skill(root, "greeter", description="says hi",
                  body="Say hello to the user.\n")
    text = skills.load_doc("greeter", [str(root)])
    assert "Say hello to the user." in text
    assert "says hi" in text          # description folded into the header
    assert "---" not in text          # frontmatter fence itself is gone


def test_load_doc_unknown_name_returns_none(tmp_path):
    root = tmp_path / "library"
    _mk_doc_skill(root, "known")
    assert skills.load_doc("unknown", [str(root)]) is None


def test_load_doc_tolerates_malformed_frontmatter(tmp_path):
    root = tmp_path / "library"
    d = root / "broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\n: : not valid yaml : :\n---\nBody text.\n")
    text = skills.load_doc("broken", [str(root)])
    assert text is not None
    assert "Body text." in text


def test_load_doc_recovers_description_with_an_embedded_colon(tmp_path):
    # Regression: a description containing its own "word: word" is common in
    # the real skill corpus and is invalid strict YAML (an unquoted colon+
    # space ends a plain scalar's value) — the bug this was written against:
    # `agentic-context-bootstrap`'s real description does exactly this
    # ("...a topic: SOUL.md's machine brief...") and came back with NO
    # description at all under a strict-only parser.
    root = tmp_path / "library"
    d = root / "real-shape"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: real-shape\n"
        "description: Use when the user asks about a topic: something else.\n"
        "version: 1.0.0\n---\nBody.\n")
    text = skills.load_doc("real-shape", [str(root)])
    assert "Use when the user asks about a topic: something else." in text


def test_listing_includes_doc_skills_with_description(tmp_path):
    root = tmp_path / "library"
    _mk_doc_skill(root, "my-doc-skill", description="a documented skill")
    out = skills.listing(None, [str(root)])
    assert "/my-doc-skill" in out
    assert "a documented skill" in out
    assert "[doc]" in out


def test_listing_executable_skill_shadows_doc_skill_of_same_name(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    _mk_skill(tmp_path / "home" / "skills", "dup.py", "# the real one\nprint('x')\n")
    root = tmp_path / "library"
    _mk_doc_skill(root, "dup", description="the doc version")
    out = skills.listing(None, [str(root)])
    assert out.count("/dup") == 1
    assert "the real one" in out
    assert "the doc version" not in out
