# Core protocol `aurora-core/1` (R499)

`aurora-rs --core [--web] --config FILE --model ID` runs Aurora 2.0's agent loop for a
front end written in another language. Today that is Python's `Engine`: the
TUI, the classic REPL, headless runs and the web page keep their Python front
ends and run each turn's loop in Rust (`aurora/rustcore.py`). Rust is the
default since R508; `runtime.core: python` (or `AURORA_CORE=python`) keeps the
Python loop, and without `aurora-rs` Aurora uses it quietly. The binary is the
first of: `AURORA_RS`, `aurora-rs` on `PATH`, this checkout's
`target/release/aurora-rs`, then `target/debug/aurora-rs`. A debug build is
used when no release build exists, so run `./install.sh` (a release build) on a
machine that serves the web page.

## What runs where
- **Python (the front end):** the prompt's secret check, budgets, compaction
  before the prompt, the `user` and `assistant` session records, `tool` records
  (from the `tool_result` events), costs, everything on screen.
- **Rust (the core):** model requests, tool calls, the approval gate with the
  allow and deny rules, checkpoints (R494), secret checks on tool output and
  writes, mid-turn compaction (R154), the extension host (R495). It writes no
  session file itself: every other record it would write (`approval`,
  `compact`) is sent as a `log` message for the front end to write.

## Transport
One JSON object per line, UTF-8, on the core's stdin (requests) and stdout
(messages). The core writes `ready` once, then answers one `turn` at a time.
Close stdin or send `shutdown` to stop it. Whatever else the core prints goes to stderr.

## Messages
| Direction | Message | Fields |
|---|---|---|
| ← | `{"ev":"ready"}` | `protocol` (`"aurora-core/1"`), `model`, `tools` (names) |
| → | `{"op":"turn"}` | `messages` (the history, ending in the new user message), `prompt` (its text: labels checkpoints), `auto_approve`, `max_iterations`, `compaction` {`auto`, `threshold_pct`, `keep_recent_tokens`, `context_limit`, `used`, `count`}, `system` (Python's system prompt, R506), `tools` (false: no tools this turn), `secret_allowlist` and `secret_session_keep` (value hashes, R507), `session_rules` ({tool: [rule]}, R507) |
| ← | `{"ev":"event"}` | `type`: `text` {chunk}, `think` {chunk}, `tool_start` {name, args}, `tool_result` {name, output, args: `{}` for a call skipped without being considered}, `usage` {input_tokens, output_tokens, cached_input_tokens}, `notice` {message} |
| ← | `{"ev":"log"}` | `event`, `data`: a session record in Python's format |
| ← | `{"ev":"approve"}` | `id`, `tool`, `args`, `diff`. Answer `y`, `n`, `a`, `d`, `s`, `c`, `t`, `o`, or `[key, note]` |
| ← | `{"ev":"continue"}` | `id`, `iterations`. Answer `true`, `false`, `["silent", ""]` or `[true, guidance]` |
| ← | `{"ev":"secret"}` | `id`, `context`, `summary` (the kinds found, never the values). Answer `keep`, `redact`, `always` or `stop` |
| ← | `{"ev":"budget"}` | `id`, after a reply with tool calls (R507). Answer `["ok"\|"warn"\|"stop", message]` |
| ← | `{"ev":"provider"}` | with `--provider-bridge` (R505): `id`, `model`, `messages`, `system`, `tools`. Stream `{"op":"provider_chunk","kind":"text"\|"think"\|"notice","chunk"}`, then answer `{"result":{…},"assistant_message":{…}}` or `{"error":…,"malformed":bool}` |
| → | `{"op":"answer"}` | `id`, `value` |
| → | `{"op":"cancel"}` | stops the running turn (Esc / Ctrl+C) |
| ← | `{"ev":"done"}` | `messages` (the history after the turn), `turn` {input_tokens, output_tokens, billed_input, cached_input, reasoning_tokens, reasoning_chars, iterations, last_request_latency, gen_tokens, gen_secs, last_output_tokens, cancelled, stopped, degraded, error, auto_approve, compactions, secret_allowlist, secret_session_keep, session_rules} |

## Not in version 1
The `e` (explain) answer at a gate and mid-turn steering: the adapter says so
(lines typed mid-turn are reported as not delivered). Plan mode and pictures:
the adapter runs such turns on the Python loop and says so.

## `--web`
For the web page (R500): file tools pass the path guard (R450) and commands are
boxed unless `runtime.sandbox: off` (R475). The page's own approval rules stay
in its front end, which answers `approve`.
