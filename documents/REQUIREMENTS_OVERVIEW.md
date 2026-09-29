# Aurora — Requirements Overview

Aurora is a micro terminal coding agent (macOS + Linux, synced via git).

> **Canonical spec is [`CHANGELOG_TECHNICAL.md`](CHANGELOG_TECHNICAL.md)**
> (formerly `AURORA.md`). It holds the full numbered requirements (R1–R293+),
> build plan, and test plan, written before the code and kept in sync with
> behaviour. This file is a stable high-level index; when the two disagree,
> `CHANGELOG_TECHNICAL.md` wins. Any behaviour change updates
> `CHANGELOG_TECHNICAL.md` (and README.md) in the same commit.

## Requirement groups (see CHANGELOG_TECHNICAL.md for the numbered detail)

- **Providers & models (R1–R5, R223).** OpenAI-compatible providers only
  (local llama.cpp / Ollama / any OpenAI-compatible server, and OpenRouter).
  A provider entry's `type: ollama` selects Ollama's own probe and
  `/api/show` context lookup (R223); everything without a `type:` uses
  llama.cpp's `/props`. Aurora consumes whatever model is loaded; it never
  launches or manages the server.
  `/model` picker; per-model `tools:` flag with graceful degrade to chat.
- **Coding agent (R6–R11).** Tool loop (read/write/edit/run/list/grep/web).
  Approval gate on writes & commands (`y`/`n`/`a`, persistent allowlist),
  diff preview before writes, a per-turn iteration cap, `!cmd` bash passthrough,
  and `/name` skills.
- **Context & memory (R12, removed).** Aurora originally had its own
  built-in `.agentic_context` integration (`context.py`/`memory.py`:
  bootstraps AGENTS.md + the three indexes + `[CORE]` docs into the system
  prompt, `/remember`, `/agentic_report`). Removed in favor of the
  `agentic_context_mcp` MCP server, which exposes the same operations
  (`read_rules`, `list_index`, `read_doc`, `write_memory`, `stats`, plus
  tagging/promotion the built-in version never had) as regular
  approval-gated MCP tools instead of hardcoded Aurora code — see
  `config.yaml`'s `mcp_servers:`. See `CHANGELOG_TECHNICAL.md` R12 for the
  historical detail.
- **TUI & UX.** Esc is the single control key (menu → cancel → exit-ask →
  clear); no accidental-exit keys; robust request cancellation (reader thread +
  socket shutdown) so a cancel lands even during prefill. The key is Esc in
  the TUI (where Ctrl+C only clears the input line) and Ctrl+C in the classic
  REPL — the cancellation MECHANISM is shared, the key is per front end.
- **Concurrency while a turn runs (R245, R246, R253, R254).** A turn in flight must
  not make the session unusable. The model can background a long command and
  poll it (`run_command(background=True)`/`check_command`/`cancel_command`);
  the human can run a `!` bash command, a read-only `/command`, `/model`, or
  ask a question and get an answer, all on a second worker. The boundary is
  fixed: nothing on that channel writes `engine.messages` or the session, so
  they keep the single writer their lock-free design assumes. A mid-turn
  question is therefore answered from a snapshot with no tools, and says so;
  a mid-turn `/model` defers its switch until the turn ends. What genuinely
  must wait still waits, but says which command and why. Two standing rules
  came out of R254's review of that machinery: nothing on the side channel
  may borrow what the turn configures by assignment or infers from its own
  output (its provider, its renderer, its think row, its transcript entry),
  and nothing on it may skip a gate the main path applies — R58's secret
  scan included.
- **Resilience.** Every backend probe is time-bounded; unreachable backends
  degrade gracefully (picker falls back to config models, sends notify to
  `/model` in ~5s) so Aurora works fully off-LAN with remote providers.

## Operating rules (project-specific)

- Repo = source of truth; runs on macOS + Linux, synced by git.
- After every shipped feature: `git add + commit + push` to Forgejo
  (`ricardo/Aurora`) without asking — multi-machine flow.
- README.md (quick start) and CHANGELOG_TECHNICAL.md (spec) must never drift
  from actual behaviour; ship doc updates in the same commit.

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) — the Engine/Frontend boundary, module
map, and data flow.
