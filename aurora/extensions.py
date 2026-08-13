"""User/bundled extensions (R119): drop a `.py` file into
`AURORA_HOME/extensions/` and its tools become callable by the model — the
SAME `SPEC`/`RUNNERS` shape `aurora/context.py` and the bundled extensions
already use internally, so writing one is "write a normal Aurora tool
module," not a new API to learn. The simplest possible extension:

    # ~/.aurora/extensions/datetime_tool.py
    from datetime import datetime, timezone

    SPEC = [{"name": "current_datetime",
             "description": "Get the current date and time (UTC).",
             "parameters": {"type": "object", "properties": {}}}]

    def current_datetime(**_):
        return datetime.now(timezone.utc).isoformat()

    RUNNERS = {"current_datetime": current_datetime}

A module that needs runtime config (the bundled MCP extension, which reads
`engine.cfg['mcp_servers']`) instead exports a `register(engine) -> (specs,
runners)` function; both static attributes and `register` may be present in
the same file and are merged. Aurora ships its own bundled extensions in
`aurora/extensions_bundled/` (loaded the same way, just from a different
directory) — MCP support lives there as the reference example for both
styles at once.

Never lets one broken extension take the rest of the session down: a file
that fails to import, or whose `register()` raises, is skipped with a
one-line warning."""

import importlib.util
import re
from pathlib import Path

from .paths import aurora_home

_BUNDLED_DIR = Path(__file__).parent / "extensions_bundled"

_TEMPLATE = '''"""{name} extension — describe what it does here."""

SPEC = [{{
    "name": "{tool_name}",
    "description": "TODO: what this tool does, in one sentence.",
    "parameters": {{
        "type": "object",
        "properties": {{
            # "arg_name": {{"type": "string", "description": "..."}},
        }},
        "required": [],
    }},
}}]


def {tool_name}(**args):
    """TODO: implement. Return a string — that's what the model sees."""
    # R171/I5: a bare `return "not implemented"` is a loadable, discoverable
    # tool that LIES to the model — it looks like a successful call that
    # happened to produce that string. Raising surfaces as `[tool error: ...]`
    # instead, which is honest about "this was never finished".
    raise NotImplementedError("{tool_name} is a scaffolded extension — edit it")


RUNNERS = {{"{tool_name}": {tool_name}}}
'''


def scaffold(name: str) -> Path:
    """Write a new extension file from the SPEC/RUNNERS template into
    `AURORA_HOME/extensions/<name>.py` (R160). Raises ValueError on a bad
    name or FileExistsError if the file is already there — never
    overwrites, the same caution `/model add` uses for config.yaml."""
    slug = re.sub(r"[^a-z0-9_]", "_", name.strip().lower())
    if not slug or slug.startswith("_"):
        raise ValueError(f"invalid extension name: {name!r}")
    d = aurora_home() / "extensions"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{slug}.py"
    if path.exists():
        raise FileExistsError(str(path))
    # R207: explicit UTF-8. `tool_name` is slugified to ASCII but `name` is
    # the user's raw text, so `/extensions new café` hit the locale encoding —
    # and write_text truncates first, leaving a 0-byte .py that the next
    # startup then tries to load as an extension.
    path.write_text(_TEMPLATE.format(name=name.strip(), tool_name=slug),
                    encoding="utf-8")
    return path


def _dirs() -> list[Path]:
    return [_BUNDLED_DIR, aurora_home() / "extensions"]


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(
        f"aurora_extension_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover(engine=None) -> tuple[list[dict], dict, list[str]]:
    """All extension tools, static + dynamic (`register(engine)`) combined,
    plus any load/register failures as plain strings — never printed here
    (this module has no UI toolkit access, R25/R90a: engine-side code never
    does terminal I/O directly). The caller (a UI-side module, e.g.
    `__main__.py`) decides how to surface them.

    `engine=None` skips the dynamic path — static-only extensions still
    load (used by anything that just wants to know what's installed)."""
    specs: list[dict] = []
    runners: dict = {}
    warnings: list[str] = []
    for d in _dirs():
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                module = _load_module(path)
            except Exception as e:
                warnings.append(f"extension {path.name} failed to load: "
                               f"{e.__class__.__name__}: {e}")
                continue
            try:
                specs.extend(getattr(module, "SPEC", None) or [])
                runners.update(getattr(module, "RUNNERS", None) or {})
                register = getattr(module, "register", None)
                if register is not None and engine is not None:
                    extra_specs, extra_runners = register(engine)
                    specs.extend(extra_specs or [])
                    runners.update(extra_runners or {})
            except Exception as e:
                warnings.append(f"extension {path.name} failed to register: "
                               f"{e.__class__.__name__}: {e}")
                continue
    return specs, runners, warnings
