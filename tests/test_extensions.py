"""Tests for aurora.extensions.discover() — name-collision consistency
between specs and runners (R227), load/register failure isolation.
scaffold() and the bundled-extension loading path already have coverage in
test_mcp.py/test_core.py; these focus on discover()'s merge logic itself,
which had no dedicated tests despite being where R227's bug lived."""

from aurora import extensions, tools


def _mk_ext(d, name, tool_name, description, return_value, order_hint=""):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{order_hint}{name}.py").write_text(f'''
SPEC = [{{"name": "{tool_name}", "description": "{description}",
        "parameters": {{"type": "object", "properties": {{}}}}}}]
def {tool_name}(**_): return "{return_value}"
RUNNERS = {{"{tool_name}": {tool_name}}}
''')


def test_colliding_tool_name_keeps_the_first_specs_matching_runner(
        tmp_path, monkeypatch):
    """R227: a colliding tool name across two extensions used to pair the
    FIRST extension's spec/description (what set_extensions() keeps) with
    the LAST extension's runner (what discover()'s .update() kept) — the
    model would be told what the first extension does but actually run the
    second's code. Must come from the SAME extension."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    ext_dir = tmp_path / "home" / "extensions"
    _mk_ext(ext_dir, "a", "greet", "politely says hello", "hello-from-a", "a_")
    _mk_ext(ext_dir, "b", "greet", "shouts loudly", "HELLO-FROM-B", "z_")

    specs, runners, warnings = extensions.discover()
    assert warnings == []
    greet_specs = [s for s in specs if s["name"] == "greet"]
    assert len(greet_specs) == 2   # discover() itself doesn't dedupe...
    # ...but the SURVIVING runner for that name must match the FIRST spec
    # (alphabetically first file, "a_greet.py"), the one set_extensions()
    # keeps after deduping the specs list.
    assert runners["greet"]() == "hello-from-a"


def test_colliding_tool_name_end_to_end_through_set_extensions(
        tmp_path, monkeypatch):
    """Same bug, verified through the real consumer: tools.set_extensions()
    dedupes specs (first wins) — the runner it ends up with for that name
    must be the one belonging to the SAME extension as the kept spec."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    ext_dir = tmp_path / "home" / "extensions"
    _mk_ext(ext_dir, "a", "greet", "politely says hello", "hello-from-a", "a_")
    _mk_ext(ext_dir, "b", "greet", "shouts loudly", "HELLO-FROM-B", "z_")

    specs, runners, _ = extensions.discover()
    tools.set_extensions(specs, runners)
    try:
        kept = next(s for s in tools._EXTENSION_SPECS if s["name"] == "greet")
        assert "politely" in kept["description"]
        assert tools.run_tool("greet", {}) == "hello-from-a"
    finally:
        tools.set_extensions([], {})


def test_register_path_collision_is_also_first_wins(tmp_path, monkeypatch):
    """The same first-wins fix must apply to the register(engine) dynamic
    path, not just the static SPEC/RUNNERS attributes."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    ext_dir = tmp_path / "home" / "extensions"
    ext_dir.mkdir(parents=True)
    (ext_dir / "a_dyn.py").write_text('''
def register(engine):
    def dyntool(**_): return "from-a"
    return ([{"name": "dyntool", "description": "a's version",
             "parameters": {"type": "object", "properties": {}}}],
            {"dyntool": dyntool})
''')
    (ext_dir / "z_dyn.py").write_text('''
def register(engine):
    def dyntool(**_): return "from-b"
    return ([{"name": "dyntool", "description": "b's version",
             "parameters": {"type": "object", "properties": {}}}],
            {"dyntool": dyntool})
''')
    specs, runners, warnings = extensions.discover(engine=object())
    # ignore the real bundled mcp_extension's own register() failure against
    # a bare object() engine — unrelated to what this test is checking
    own_warnings = [w for w in warnings if "dyn.py" in w]
    assert own_warnings == []
    assert runners["dyntool"]() == "from-a"


def test_no_collision_both_extensions_keep_their_own_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    ext_dir = tmp_path / "home" / "extensions"
    _mk_ext(ext_dir, "a", "tool_a", "does a", "result-a")
    _mk_ext(ext_dir, "b", "tool_b", "does b", "result-b")
    specs, runners, warnings = extensions.discover()
    assert warnings == []
    assert runners["tool_a"]() == "result-a"
    assert runners["tool_b"]() == "result-b"


# ── failure isolation ────────────────────────────────────────────────────

def test_a_broken_extension_does_not_stop_others_from_loading(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    ext_dir = tmp_path / "home" / "extensions"
    ext_dir.mkdir(parents=True)
    (ext_dir / "a_broken.py").write_text("this is not valid python (((\n")
    _mk_ext(ext_dir, "b", "works", "fine", "ok", "z_")
    specs, runners, warnings = extensions.discover()
    assert any("a_broken.py" in w and "failed to load" in w for w in warnings)
    assert runners["works"]() == "ok"


def test_a_register_that_raises_does_not_lose_the_extensions_own_static_tools(
        tmp_path, monkeypatch):
    """A register() failure should only drop that extension's DYNAMIC
    contribution — its own static SPEC/RUNNERS (declared above register in
    the same file) must survive, since they never depended on register()
    succeeding."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    ext_dir = tmp_path / "home" / "extensions"
    ext_dir.mkdir(parents=True)
    (ext_dir / "mixed.py").write_text('''
SPEC = [{"name": "static_tool", "description": "static",
        "parameters": {"type": "object", "properties": {}}}]
def static_tool(**_): return "static-ok"
RUNNERS = {"static_tool": static_tool}

def register(engine):
    raise RuntimeError("register is broken")
''')
    specs, runners, warnings = extensions.discover(engine=object())
    assert any("mixed.py" in w and "failed to register" in w for w in warnings)
    assert runners["static_tool"]() == "static-ok"


def test_register_is_skipped_entirely_when_engine_is_none(tmp_path, monkeypatch):
    """discover(engine=None) is the static-only mode — register() must not
    even be called, so a register() that would raise never gets the chance."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    ext_dir = tmp_path / "home" / "extensions"
    ext_dir.mkdir(parents=True)
    (ext_dir / "mixed.py").write_text('''
SPEC = [{"name": "static_tool", "description": "static",
        "parameters": {"type": "object", "properties": {}}}]
def static_tool(**_): return "static-ok"
RUNNERS = {"static_tool": static_tool}

def register(engine):
    raise RuntimeError("must never be called")
''')
    specs, runners, warnings = extensions.discover(engine=None)
    assert warnings == []
    assert runners["static_tool"]() == "static-ok"


def test_underscore_prefixed_files_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    ext_dir = tmp_path / "home" / "extensions"
    ext_dir.mkdir(parents=True)
    (ext_dir / "_helper.py").write_text('''
SPEC = [{"name": "hidden", "description": "d",
        "parameters": {"type": "object", "properties": {}}}]
RUNNERS = {"hidden": lambda **_: "x"}
''')
    specs, runners, warnings = extensions.discover()
    assert warnings == []
    assert "hidden" not in runners


def test_bundled_dir_loads_before_user_extensions_dir(tmp_path, monkeypatch):
    """_dirs() lists _BUNDLED_DIR first — a user extension colliding with a
    real bundled tool name (lint_extension's "lint_check") must NOT
    silently win: bundled loads first, so first-wins keeps the bundled
    runner, consistent with R227's fix."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    ext_dir = tmp_path / "home" / "extensions"
    _mk_ext(ext_dir, "fake_lint", "lint_check", "user override attempt",
           "user-version")
    specs, runners, warnings = extensions.discover()
    assert "lint_check" in runners
    assert runners["lint_check"]() != "user-version"


# ── R238: a spec with no runner must never reach the model ───────────────

def test_a_spec_without_a_runner_is_skipped_with_a_warning():
    """R238: same defect as the nameless-spec guard — advertised to the
    model and permanently uncallable — reached through a different door.
    It used to be kept silently, and every call answered
    `[error: unknown tool 'ghost']`."""
    warnings = tools.set_extensions(
        [{"name": "ghost", "description": "x", "parameters": {}}], {})
    assert any("no runner" in w for w in warnings)
    assert "ghost" not in [s["name"] for s in tools.specs()]


def test_a_paired_spec_and_runner_is_still_kept():
    warnings = tools.set_extensions(
        [{"name": "real_tool", "description": "x", "parameters": {}}],
        {"real_tool": lambda **_: "ok"})
    assert warnings == []
    assert tools.run_tool("real_tool", {}) == "ok"
    tools.set_extensions([], {})


def test_only_the_runnerless_spec_is_dropped_from_a_mixed_extension():
    """A file exporting two tools but a runner for only one keeps the good
    one — one typo must not disable the whole extension."""
    warnings = tools.set_extensions(
        [{"name": "good_tool", "description": "x", "parameters": {}},
         {"name": "typoed_tool", "description": "x", "parameters": {}}],
        {"good_tool": lambda **_: "fine"})
    names = [s["name"] for s in tools.specs()]
    assert "good_tool" in names and "typoed_tool" not in names
    assert len(warnings) == 1
    tools.set_extensions([], {})


def test_scaffold_writes_atomically(tmp_path, monkeypatch):
    """R238: R207's comment named the truncation but fixed only the
    encoding — an interrupted scaffold must leave no file, not a
    half-written .py the next startup imports."""
    monkeypatch.setattr(extensions, "aurora_home", lambda: tmp_path)
    path = extensions.scaffold("demo tool")
    assert path.read_text(encoding="utf-8").startswith('"""demo tool')
    # no dangling temp files next to it
    assert sorted(p.name for p in path.parent.iterdir()) == ["demo_tool.py"]
