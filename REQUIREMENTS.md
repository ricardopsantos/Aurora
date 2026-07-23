# Aurora — Requirements

Aurora is a micro terminal coding agent (macOS + Linux, synced via git).

> **Canonical spec is [`AURORA.md`](AURORA.md).** It holds the full numbered
> requirements (R1–R90), build plan, and test plan, written before the code
> and kept in sync with behaviour. This file is a stable high-level index; when
> the two disagree, AURORA.md wins. Any behaviour change updates AURORA.md (and
> README.md) in the same commit.

## Requirement groups (see AURORA.md for the numbered detail)

- **Providers & models (R1–R5).** OpenAI-compatible providers only (local
  llama.cpp / any OpenAI-compatible server, and OpenRouter). Aurora consumes
  whatever model is loaded; it never launches or manages the server. Optional
  local library switching via LlamaDesk with an explicit eviction confirm.
  `/model` picker; per-model `tools:` flag with graceful degrade to chat.
- **Coding agent (R6–R11).** Tool loop (read/write/edit/run/list/grep/context/
  web). Approval gate on writes & commands (`y`/`n`/`a`, persistent allowlist),
  diff preview before writes, a per-turn iteration cap, `!cmd` bash passthrough,
  and `/name` skills.
- **Context & memory (R12+).** agentic_context protocol: bootstraps the
  project's context folder (AGENTS.md rules+personality, the three indexes,
  `[CORE]` docs) so the user shapes Aurora through context, not code. The
  folder is found by its CONTENTS, never its name (R88/R90c) — the nearest
  ancestor, walking up from the cwd, holding a subfolder with both
  `KNOWLEDGE/SKILL.md` and `MEMORY/SKILL.md`; `.agentic_context` is the
  convention, not a requirement.
- **TUI & UX.** Esc is the single control key (menu → cancel → exit-ask →
  clear); no accidental-exit keys; robust request cancellation (reader thread +
  socket shutdown) so Esc/Ctrl+C interrupt even during prefill.
- **Resilience.** Every backend probe is time-bounded; unreachable backends
  degrade gracefully (picker falls back to config models, sends notify to
  `/model` in ~5s) so Aurora works fully off-LAN with remote providers.

## Operating rules (project-specific)

- Repo = source of truth; runs on macOS + Linux, synced by git.
- After every shipped feature: `git add + commit + push` to Forgejo
  (`ricardo/Aurora`) without asking — multi-machine flow.
- README.md (quick start) and AURORA.md (spec) must never drift from actual
  behaviour; ship doc updates in the same commit.

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) — the Engine/Frontend boundary, module
map, and data flow.
