# Aurora Extensions

Drop a `.py` file into `~/.aurora/extensions/` and its tools become
callable by the model — no new API to learn, just the same `SPEC` +
`RUNNERS` shape Aurora's own built-in tools (`aurora/context.py`) and its
bundled extensions already use internally.

## The simplest possible extension

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

That's the whole file. Restart Aurora, and the model can call
`current_datetime`. `SPEC` is a list of tool definitions (name,
description, JSON-schema parameters — identical shape to every built-in
tool); `RUNNERS` maps each tool's name to the function that runs it. A
runner is called with its arguments as keywords (`fn(**args)`), and must
return a string — raise if something goes wrong, Aurora converts any
exception into a `[tool error: ...]` result fed back to the model rather
than letting it kill the turn.

## Two ways to register tools

**Static** (the example above): module-level `SPEC` and `RUNNERS`. Use
this whenever the tool doesn't depend on anything from `config.yaml` or
the running session — it's fixed the moment the file is imported.

**Dynamic**: a module-level function

```python
def register(engine):
    ...
    return spec_list, runners_dict
```

Use this when the tool set depends on runtime config — Aurora's own
bundled MCP extension (below) is the reference example, since which tools
exist depends entirely on what's listed under `mcp_servers:`.

A single file may define both static `SPEC`/`RUNNERS` *and* a `register()`
function; everything gets merged.

## Where extensions live

- `AURORA_HOME/extensions/*.py` — yours. Global only for now: every
  project sees the same set. (No project-local `.aurora/extensions/` yet —
  that would need a first-run trust prompt, similar to the bootstrap
  prompt's "run it?" confirmation, since a project-local extension is code
  a repository could plant without your explicit install step.)
- `aurora/extensions_bundled/*.py` — ships with Aurora itself. The only
  thing that makes one of these "built in" rather than user-authored is
  which directory it lives in; the loading mechanism is identical.

Both directories are flat — one file per extension, no subfolder, no
manifest. That's deliberate for now: it matches the simplest case (one
tool, no dependencies) and nothing bundled with Aurora today needs more
than that. If an extension ever needs multiple files (a helper module, a
bundled data file, its own third-party dependency), that's the point to
revisit this.

## Loading behavior

Extensions are discovered **once per session**, at `Engine` construction —
not re-scanned per turn. This matters most for anything dynamic: the
bundled MCP extension spawns a child process per configured server, and
that must happen once, not on every message.

A file that fails to import, or a `register()` that raises, is skipped —
never fatal to the rest of the session. What went wrong is collected as a
plain string on `engine.extension_warnings` and printed once at startup;
extension code itself never prints directly (it runs on the engine side of
Aurora's UI/engine boundary, which does no terminal I/O of its own).

## Security

**Extensions run with Aurora's own process permissions and can execute
arbitrary code. Only install ones you trust.** There is no sandboxing,
no permission model narrower than "whatever the user running Aurora can
do" — the same posture most agent-extension systems take (this is
explicitly true of Pi's TypeScript extensions too, which this design was
partly inspired by).

## Bundled extensions

Aurora ships four extensions by default, in `aurora/extensions_bundled/`:
the MCP client, web search/fetch, price refresh, and the lint checker.

### MCP client

A client for the [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) — `aurora/extensions_bundled/mcp_extension.py`, backed by
`aurora/mcp.py`.

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

- **stdio transport only** — Aurora spawns each server as a child process
  and speaks newline-delimited JSON-RPC 2.0 over its stdin/stdout, per the
  MCP spec. No remote SSE/HTTP servers yet.
- Each server's tools are discovered via the `initialize` → `tools/list`
  handshake and exposed to the model as `mcp_<server>_<tool>` — the prefix
  keeps two servers from registering colliding names.
- **`env` maps a variable name the child process expects to an Aurora
  keystore variable name** (`aurora key set <name>`) — credentials are
  resolved through the same env → OS keyring → encrypted file chain every
  other Aurora key uses, never plaintext in `config.yaml`. Resolution is
  non-interactive at startup: a missing credential means that server will
  likely report its own auth error, not that Aurora hangs waiting for
  input while constructing the engine.
- One misconfigured or crashing server never takes down the others, or
  Aurora itself — a broken entry is recorded in `engine.extension_warnings`
  (prefixed `mcp: ...`) and the rest still load.
- Server processes are cleaned up automatically when Aurora exits.

### The one non-negotiable rule: every MCP tool call needs approval

MCP has no universal "this tool is read-only/safe" flag the way Aurora's
own built-in tools do. Aurora's entire safety model is approval-gated
writes — every `write_file`/`edit_file`/`run_command`/`apply_patch` shows
a preview and asks first. Silently trusting a configured MCP server would
undercut that completely, so **every single `mcp_*` call is gated, with no
"trusted server" bypass** (`tools.needs_approval()` — a prefix check,
since which tools exist isn't known until each server's handshake
completes). You can still "always allow" a specific one from the approval
prompt, the same way you'd allowlist a `run_command` pattern — the
*default*, with no rule yet, is always "ask."

### Price refresh

`refresh_model_prices` — `aurora/extensions_bundled/price_refresh_extension.py`.
Re-pulls price/context-size info from OpenRouter's public catalog for every
`provider: openrouter` entry in config.yaml's `models:` list — the batch
counterpart to `/model add`'s automatic one-model lookup, for models added
before pricing existed or whose listed price has since changed. The local
model is skipped (no OpenRouter listing, no price); a configured model no
longer on OpenRouter's catalog is reported as skipped, not an error. Model-
callable, not automatic — same posture as `lint_check` below.

Opening `/model` now does the same refresh on its own, in the background and
at most once a day per model, so this tool is the way to force one *now* —
past the daily TTL, or when you want the result reported in the chat rather
than just reflected in the picker's rows.

### Web search & fetch

`web_search(query, max_results=5)` and `web_fetch(url)` —
`aurora/extensions_bundled/web_extension.py`. Search goes through
[`ddgs`](https://pypi.org/project/ddgs/) (DuckDuckGo, no API key); fetch is
`httpx` plus a crude HTML→text pass. Both are **read-only, so neither is
approval-gated** (R6) — unlike every `mcp_*` tool above.

Two caps keep a hostile or merely enormous page bounded: the body is
streamed and cut at 2MB (a plain `.get()` would pull an arbitrarily large
response into memory before anything could truncate it), and the extracted
text is trimmed to 20 000 characters with an explicit `[truncated]` marker.
A missing `ddgs` degrades to `[web_search unavailable: ddgs not installed]`
rather than failing the turn.

Dynamic `register(engine)` rather than a static `SPEC`/`RUNNERS` pair,
because it honours `runtime.web_search`: with the flag off it contributes
nothing at all, so the model never sees a tool it isn't allowed to call. A
static extension loads unconditionally, which would have turned
`web_search: false` into a no-op. This was `aurora/websearch.py`, wired into
the engine by name, until R157 moved it out — living in
`extensions_bundled/` is now the only thing that makes it "built in."

### Lint checker

`lint_check(path)` — `aurora/extensions_bundled/lint_extension.py`. A tool
the model can call after writing or editing a `.py` file to catch bugs,
unused imports, and style problems before they ever reach you. Inspired by
comparing against a competing agent's `pi-lens` extension (full LSP
diagnostics, tree-sitter structural analysis, multi-language formatting) —
deliberately scoped **way** down from that:

- **Python only, for now.** No LSP client, no bundled parsers for other
  languages.
- Uses [`ruff`](https://docs.astral.sh/ruff/) if it's on `$PATH` — real
  linting, real style/bug findings. If `ruff` isn't installed, falls back
  to a syntax-only check via Python's own `py_compile` (always available,
  no setup) and says so explicitly, rather than silently doing a much
  weaker check and implying it was a real lint pass.
- Static `SPEC`/`RUNNERS` — the simplest possible shape, same as this
  doc's `current_datetime` example, just with a real subprocess behind it.

**This is a model-callable tool, not an automatic hook.** It only runs
when the model chooses to call it — nudged by the tool's own description
("call this after writing or editing a .py file"), the same way any other
tool relies on the model choosing to use it (`web_search`, `grep`, etc.).
A guaranteed "runs after every single write/edit, no matter what" version
would need Aurora's extension mechanism to support lifecycle hooks first —
see "What's scoped out" below; that's not built yet.

## What's scoped out (for now)

Compared to a fuller extension system (Pi's TypeScript extensions were the
reference point for what's possible):

- No lifecycle-hook system (an extension can't subscribe to "before/after
  tool call," "session started," etc. — only register callable tools).
- No custom `/commands` or keybindings from an extension.
- No custom TUI rendering.
- No project-local extensions or a trust-prompt flow for them.
- MCP: stdio transport only, no remote SSE/HTTP servers.

This is Phase 1 of a larger plan — real, working, and useful today, not
the ceiling of what the mechanism could become.
