# Aurora extensions

Drop a `.py` file into `~/.aurora/extensions/` and its tools become available
to the model. There's no new API to learn: an extension uses the same `SPEC`
and `RUNNERS` shape as Aurora's built-in tools (`aurora/context.py`) and its
bundled extensions.

## The simplest extension

```python
# ~/.aurora/extensions/datetime_tool.py
from datetime import datetime, timezone

SPEC = [{
    "name": "current_datetime",
    "description": "Get the current date and time (UTC).",
    "parameters": {"type": "object", "properties": {}},
}]

def current_datetime(**_):
    return datetime.now(timezone.utc).isoformat()

RUNNERS = {"current_datetime": current_datetime}
```

That's the whole file. Restart Aurora and the model can call
`current_datetime`.

`SPEC` is a list of tool definitions: name, description and JSON-schema
parameters, the same shape as every built-in tool. `RUNNERS` maps each tool
name to the function that runs it. Aurora calls a runner with its arguments
as keywords (`fn(**args)`), and the runner must return a string. If something
goes wrong, raise an exception. Aurora turns it into a `[tool error: ...]`
result for the model, and the turn carries on.

## Two ways to register tools

Static (the example above): module-level `SPEC` and `RUNNERS`. Use this when
the tool doesn't depend on `config.yaml` or the running session. It's fixed
when the file is imported.

Dynamic: a module-level function

```python
def register(engine):
    ...
    return spec_list, runners_dict
```

Use this when the set of tools depends on the config. The bundled MCP
extension (below) is the example: which tools exist depends on what's listed
under `mcp_servers:`.

One file can have both a static `SPEC`/`RUNNERS` and a `register()`
function. Aurora merges them.

## Where extensions live

- `AURORA_HOME/extensions/*.py` holds yours. They're global, so every project
  sees the same set. Project-local extensions (`.aurora/extensions/`) aren't
  supported, because a repository could plant code there without you
  installing it; they would need a trust prompt first, like the bootstrap
  prompt's "run it?" question.
- `aurora/extensions_bundled/*.py` holds the ones that ship with Aurora.
  They load the same way as yours; only the folder is different.

Both folders are flat: one file per extension, no subfolders and no
manifest. That covers the simple case (one tool, no dependencies), and none
of the bundled extensions needs more. An extension that needs several files
or its own dependencies would need a different layout.

## Loading

Aurora finds extensions once per session, when the `Engine` is created, not
on every turn. This matters for dynamic extensions: the MCP extension starts
a child process for each configured server, and that should happen once.

A file that fails to import, or a `register()` that raises, is skipped and
the session carries on. The error is added as a plain string to
`engine.extension_warnings` and printed once at startup. Extension code never
prints anything itself, because it runs on the engine side, which does no
terminal output.

## Security

Extensions run with Aurora's own permissions and can run any code. Only
install extensions you trust. There is no sandbox and no permission model
narrower than "whatever the user running Aurora can do".

## Bundled extensions

Aurora ships four extensions in `aurora/extensions_bundled/`: the MCP client,
web search and fetch, price refresh, and the lint checker.

### MCP client

A client for the [Model Context Protocol](https://modelcontextprotocol.io)
(MCP), in `aurora/extensions_bundled/mcp_extension.py` and `aurora/mcp.py`.

Configure servers in `config.yaml`:

```yaml
mcp_servers:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allow"]
  - name: github
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env: {GITHUB_PERSONAL_ACCESS_TOKEN: GITHUB_TOKEN}
```

- Only the stdio transport is supported. Aurora starts each server as a
  child process and talks newline-delimited JSON-RPC 2.0 over its stdin and
  stdout, as the MCP spec describes. Remote SSE and HTTP servers aren't
  supported.
- Aurora finds each server's tools with the `initialize` and `tools/list`
  handshake and shows them to the model as `mcp_<server>_<tool>`. The prefix
  stops two servers from clashing on a tool name.
- `env` maps a variable the server expects to an Aurora key name (as in
  `aurora key set <name>`). Aurora resolves it through the same chain as
  every other key (environment, OS keyring, encrypted file), so credentials
  never sit in plain text in `config.yaml`. This happens at startup without
  asking anything; if a credential is missing, that server will probably
  report its own login error, and Aurora won't hang waiting for input.
- A server that is misconfigured or crashes doesn't affect the others or
  Aurora. The problem is added to `engine.extension_warnings` (starting with
  `mcp: ...`) and the other servers still load.
- Aurora stops the server processes when it exits.

#### Every MCP tool call asks first

MCP has no reliable "this tool is read-only" flag like Aurora's built-in
tools have. Aurora's safety model is that writes ask first: every
`write_file`, `edit_file`, `run_command` and `apply_patch` shows a preview
and asks. Trusting an MCP server blindly would undo that, so every `mcp_*`
call asks, and there is no "trusted server" setting. The check in
`tools.needs_approval()` matches the prefix, because the tool names aren't
known until each server's handshake finishes. You can still pick "always
allow" for a specific tool at the approval prompt, as you would for a
`run_command` pattern; without a rule, Aurora asks.

### Price refresh

`refresh_model_prices`, in
`aurora/extensions_bundled/price_refresh_extension.py`. It fetches the price
and context size from OpenRouter's public catalog for every
`provider: openrouter` entry under `models:` in `config.yaml`. `/model add`
does this for one model when you add it; this tool does it for all of them.
The local model is skipped, since it has no OpenRouter listing or price. A
configured model that is no longer in the catalog is reported as skipped,
not as an error.

Opening `/model` also refreshes prices in the background, at most once a day
per model. Use this tool to force a refresh now, or to see the result in the
chat. The model calls it when asked; it doesn't run by itself.

### Web search and fetch

`web_search(query, max_results=5)` and `web_fetch(url)`, in
`aurora/extensions_bundled/web_extension.py`. Search uses
[`ddgs`](https://pypi.org/project/ddgs/) (DuckDuckGo, no API key needed).
Fetch uses `httpx` and turns the page into readable text.

Both are read-only, so they don't ask for approval, with one exception:
`web_fetch` asks before fetching a private or local address. It decides what
counts as private the same way at the prompt and at fetch time, and it
refuses an address that only turns private after approval (DNS rebinding).

Two limits keep a hostile or very large page under control. The body is
streamed and cut off at 2 MB, so a huge response never fills memory, and the
extracted text is trimmed to 20,000 characters with a `[truncated]` marker.
If `ddgs` isn't installed, `web_search` returns
`[web_search unavailable: ddgs not installed]` and the turn carries on.

These tools use `register(engine)` instead of a static `SPEC`/`RUNNERS`,
because they follow `runtime.web_search`. With it off they add nothing, so
the model never sees a tool it isn't allowed to use. A static extension
always loads, which would make `web_search: false` do nothing.

### Lint checker

`lint_check(path)`, in `aurora/extensions_bundled/lint_extension.py`. The
model can call it after writing or editing a `.py` file to catch bugs, unused
imports and style problems before you see them.

- Python only. There is no LSP client and no parser for other languages.
- If [`ruff`](https://docs.astral.sh/ruff/) is on `$PATH`, it runs a real
  lint. If not, it falls back to a syntax-only check with Python's own
  `py_compile`, and says so, so a weaker check never passes as a full lint.
- It uses a static `SPEC`/`RUNNERS`, like the `current_datetime` example,
  with a subprocess behind it.

It's a tool the model chooses to call, not an automatic step. Its
description says "call this after writing or editing a .py file", and the
model decides, as with `web_search` or `grep`. Running it after every write
would need lifecycle hooks, which extensions don't have (see below).

## Not supported yet

- Lifecycle hooks. An extension can only register tools; it can't react to
  events such as "before a tool call" or "session started".
- Custom `/commands` or key bindings from an extension.
- Custom TUI rendering.
- Project-local extensions and a trust prompt for them.
- MCP over remote SSE or HTTP; only stdio works.
