"""Bundled default extension (R119): connects Aurora to every MCP server
configured under `config.yaml`'s `mcp_servers:`, exposing each one's tools
to the model alongside Aurora's own built-ins.

This ships with Aurora (living in `aurora/extensions_bundled/` instead of
`~/.aurora/extensions/` is the only thing that makes it "built in" rather
than user-installed) and doubles as the reference example for the dynamic
`register(engine)` extension style — used here because the tool set is
config-driven (which MCP servers, if any) rather than fixed at import time
like a static `SPEC`/`RUNNERS` extension (see `aurora/extensions.py`'s
docstring for that simpler style).
"""


def register(engine):
    servers = engine.cfg.get("mcp_servers") or []
    if not servers:
        return [], {}
    from aurora import mcp
    manager = mcp.MCPManager(servers)
    # kept alive for the session (atexit closes it); manager.errors (per-
    # server startup failures) surfaces through engine.extension_warnings,
    # same as any other extension issue — no print() here, this file runs
    # on the engine side (R25/R90a: no terminal I/O).
    engine._mcp_manager = manager
    return manager.specs(), manager.runners()
