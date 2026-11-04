# MCP servers

Aurora's MCP client is described in
[`EXTENSIONS.md`](EXTENSIONS.md#mcp-client). This page explains how to add a
server.

Aurora supports only the stdio transport, not remote SSE or HTTP MCP servers
(see `EXTENSIONS.md`). Every tool a server provides goes through the approval
prompt.

## Adding a server

Each server is its own project, with its own virtualenv
(`python3 -m venv .venv && .venv/bin/pip install -e .`) and its own entry
point (for a Python server, `python -m <package>.server`). Add it under
`mcp_servers:` in `config.yaml`, with `command` pointing at that venv's
Python and `args` at the module. Each server's README lists the exact
arguments and environment variables.

If a tool is only useful to Aurora, an extension (a single `.py` file in
`~/.aurora/extensions/`) is usually simpler than a separate MCP server.
`EXTENSIONS.md` explains the trade-off.
