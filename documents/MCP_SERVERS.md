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

## Limiting which tools a server exposes

Every tool a server advertises is sent to the model on every request, so a
server with dozens of tools can cost thousands of tokens per turn. List only
the ones you want with `tools:`:

```yaml
mcp_servers:
  - name: memory
    command: /path/to/server
    tools: [remember, recall]   # the server's own tool names, not mcp_memory_*
```

Tools you do not list are not shown to the model and cannot be called. With
no `tools:` key you get every tool. A `tools:` value that is not a list of
names exposes nothing from that server (and warns), and a name the server
does not have only warns. Use the server's own tool names (`recall`), not
the prefixed ones Aurora shows (`mcp_memory_recall`).


## Example: an external memory (Mnemosyne)

Aurora works fully without any external memory. Written rules, decisions and
project context belong in plain files (for example an
Agentic Context folder); an external memory is only for
**episodic recall**: facts and past conversation snippets found by meaning
rather than by file name. It is optional and nothing in Aurora depends on it.

[Mnemosyne](https://github.com/AxDSan/mnemosyne) (MIT licence) is one such
store: a local SQLite database with keyword and embedding search, and no
outside calls. It ships an MCP server (`mnemosyne mcp`, stdio). Install it in
its own virtualenv, then add it with a `tools:` allowlist, because it
advertises 36 tools (about 6,500 tokens per request) and four are enough:

```yaml
mcp_servers:
  - name: mnemosyne
    command: /path/to/mnemosyne-venv/bin/mnemosyne
    args: ["mcp"]
    timeout: 90                 # the first start loads the embedding model
    tools: [mnemosyne_remember, mnemosyne_recall, mnemosyne_get, mnemosyne_forget]
```

The tools then appear as `mcp_mnemosyne_mnemosyne_recall` and so on (about
1,100 tokens in total). Point the server at its own data directory with
`MNEMOSYNE_DATA_DIR`; check the Mnemosyne README for the other variables and
the embedding model download (several hundred MB).

Advice:

- **Start with an empty store.** Do not load another tool's database as is:
  automatic capture stores whatever was said, including test prompts.
- Model-written memories are not reviewed. Treat a recalled fact as a hint to
  verify, not as a rule.
- Every call still goes through the approval prompt.
