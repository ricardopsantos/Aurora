# Extension protocol `aurora-ext/1` (R477)

Aurora's Python extensions (`SPEC` / `RUNNERS` / `register(engine)` modules in
`~/.aurora/extensions/`, see `aurora/extensions.py`) can run behind a process
boundary. `python -m aurora.exthost` loads them and serves their tools over
newline-delimited JSON-RPC 2.0 on stdin and stdout.

This is the compatibility layer for Aurora 2.0: its Rust core starts the host
as a subprocess and keeps every existing extension working without embedding
Python. A Python Aurora can use it too, to keep an extension it does not trust
out of its own process.

## Transport
- One JSON object per line, UTF-8, on the host's stdin (requests) and stdout
  (responses). A line is at most 16 MiB.
- stdout carries nothing else: whatever an extension prints, at import or in a
  call, goes to stderr.
- The host answers requests one at a time, in order. Close stdin, or send
  `shutdown`, to stop it; it exits 0.

## Methods
| Method | Params | Result |
|---|---|---|
| `initialize` | `protocol` (`"aurora-ext/1"`), `paths` (files or folders of `.py` extensions; names starting with `_` are skipped), `config` (the parsed config.yaml) | `protocol`, `tools` (`[{name, description, parameters}]`, JSON Schema parameters), `warnings` (one line per file that failed to load or register, or tool skipped) |
| `tools/list` | none | `tools` |
| `tools/call` | `name`, `arguments` (an object) | `output` (a string: what the model sees), `is_error` (true when the tool raised; `output` is then `[tool error: Type: message]`) |
| `shutdown` | none | `{}` |

## Errors
JSON-RPC errors: `-32700` not JSON, `-32600` not a request (or too large),
`-32601` unknown method, `-32602` bad params (unknown tool, arguments not an
object, unsupported protocol version), `-32002` a call before `initialize`.
A tool that raises is not a protocol error: it answers `is_error: true`.

## Rules the host keeps (same as in process)
- Files load in name order; a tool name already defined is skipped with a
  warning (the first one wins, R227). A spec without a runner is skipped.
- `register(engine)` receives an object with only `cfg` (the `config` sent in
  `initialize`) and `runtime` (`cfg["runtime"]`). An extension that needs more
  of the engine runs in process only.
- A file that fails to import, or a `register()` that raises, is a warning;
  the other extensions still load.

## Not in version 1
Streaming output, cancellation of a running call, and calls from the
extension back into Aurora. A later version adds them as new methods; a host
refuses an `initialize` for a protocol it does not speak, so both sides find
out at once.
