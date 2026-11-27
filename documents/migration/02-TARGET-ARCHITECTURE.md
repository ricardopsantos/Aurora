# Target architecture

```
crates/
  aurora-core       contracts and the agent loop, no I/O of its own
  aurora-storage    AURORA_HOME, config, sessions, keys, approval rules
  aurora-providers  model APIs (OpenAI-compatible first)
  aurora-tools      built-in tools
  aurora-cli        the aurora-rs binary (headless now; the TUI later)
```

Dependencies point one way: `cli` uses everything, and `storage`, `providers`
and `tools` use only `core`. `core` uses no other crate of the workspace.

## Contracts (`aurora-core`)

- **Provider**: `turn(request, sink, cancel) -> TurnResult`. It streams text and
  reasoning through `sink` and returns the tool calls and the usage. It also
  shapes the messages for its API (`assistant_message`,
  `tool_result_message`), so the loop never knows the wire format.
- **Frontend**: `event(AgentEvent)` for everything shown, plus `approve`,
  `ask_continue` and `cancel`. `AgentEvent` serialises to JSON, so the web page
  and remote clients get the same stream as the terminal.
- **Tool / ToolBox**: a spec for the model, `needs_approval`, `parallel_safe`,
  a preview for the approval prompt, and `run`. The result is always a string,
  capped at 60 000 characters as in Python.
- **ApprovalPolicy**: `is_denied`, `is_allowed`, the "always" rule writers, and
  `complete()`. A policy that can't apply all of the user's rules says so, and
  auto-approve then asks instead.
- **SessionLog**: `log(event, fields)`, the session's records.
- **ext**: `aurora-ext/1` JSON-RPC types for Python extensions behind a process
  boundary.

## The loop

`Agent::send` logs the user record, then calls `run_turn`, then logs the
assistant record with the same fields Python logs. `run_turn` loops: model
turn → tool calls → for each one: denylist, ask unless allowed or
auto-approved, run (reads in parallel), log → next round. The loop ends when
there are no calls left, the user cancels or stops, or the iteration cap is
reached and the front end declines to go on. Every call the model made gets
exactly one result in the history, whatever happened, so the next request is
always valid.
