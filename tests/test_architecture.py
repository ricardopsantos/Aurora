"""Architecture guard — makes the UI/engine boundary executable, not just
documented. These tests fail if the engine ever grows a UI dependency, so you
can improve the engine or swap the UI without silently breaking the split.

The rule:
  ENGINE modules must not import any UI toolkit (prompt_toolkit) and must not
  do terminal I/O (input(), or print() to talk to a human). The engine reaches
  the outside world ONLY through frontend.Frontend and keystore.set_prompter.
"""

import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parent.parent / "aurora"

# Modules that ARE the UI or its wiring — exempt from the engine rule.
UI_MODULES = {"ui.py", "tui.py", "clipboard.py", "__main__.py"}
# Toolkits only a UI may import.
FORBIDDEN_IMPORTS = {"prompt_toolkit"}


def _engine_files():
    return [p for p in PKG.rglob("*.py") if p.name not in UI_MODULES]


def test_engine_never_imports_ui_toolkit():
    offenders = []
    for path in _engine_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.Import):
                mod = node.names[0].name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            if mod and mod.split(".")[0] in FORBIDDEN_IMPORTS:
                offenders.append(f"{path.name}: imports {mod}")
    assert not offenders, "engine importing UI toolkit:\n" + "\n".join(offenders)


def test_engine_never_imports_concrete_ui_module():
    """Catches RELATIVE imports too (R90a). `from .ui import x` parses as
    module='ui', level=1 — an endswith('.ui') check on `module` alone never
    matched it, and exactly that import (engine.py reaching for
    ui.estimate_tokens) sat in the engine unnoticed while this test passed."""
    for path in _engine_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                dotted = "." * (node.level or 0) + (node.module or "")
                assert not dotted.endswith((".ui", ".clipboard")), \
                    f"{path.name}:{node.lineno} imports a concrete UI module ({dotted})"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.endswith((".ui", ".clipboard")), \
                        f"{path.name}:{node.lineno} imports a concrete UI module"


def test_engine_does_no_terminal_io():
    """No input() and no bare print() in engine modules — human interaction
    goes through the Frontend. (subprocess output isn't terminal I/O.)"""
    offenders = []
    for path in _engine_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"input", "print"}:
                    offenders.append(f"{path.name}:{node.lineno} calls {node.func.id}()")
    assert not offenders, "engine doing terminal I/O:\n" + "\n".join(offenders)
