# Aurora requirements overview

Aurora is a micro coding agent for the terminal and the browser. It runs on
macOS and Linux.

This page is a short map of the requirement groups. The numbered detail lives
in two places:

- [`CHANGELOG_TECHNICAL.md`](CHANGELOG_TECHNICAL.md): R1 to R293, with the
  original build plan and test plan.
- [`REQUIREMENTS.md`](../REQUIREMENTS.md) and the files under
  [`requirements/`](../requirements/): the newer requirements as testable
  rules, from R223 up to R440 and beyond. New requirements go here.

If this page disagrees with either of them, they're right.

## Requirement groups

- **Providers and models (R1 to R5, R223, R295 to R345).** Aurora works with
  OpenAI-compatible providers: a local llama.cpp server, Ollama, any other
  OpenAI-compatible server, and OpenRouter. A provider with `type: ollama`
  uses Ollama's own health check and `/api/show` for the context size;
  others use llama.cpp's `/props`. Aurora uses whatever model the server has
  loaded and never starts or manages the server. The `/model` picker lists
  every model, can add one from an OpenRouter URL, and marks a sleeping local
  model as "asleep". Each model has a `tools:` flag; a model without tools
  falls back to plain chat. A machine can override the shared config with
  `config.local.yaml`.
- **The coding agent (R6 to R11).** The tool loop reads, writes, edits, runs
  commands, lists, greps and searches the web. Writes and commands ask first,
  with a diff preview, and the answers can be saved as allow or deny rules.
  Each turn has a step limit. `!` runs a shell command directly, and `/name`
  runs a skill.
- **Approvals (R149, R319, R334 to R340).** How far an "always allow" rule
  reaches depends on how risky the command is. You can also allow for this
  session only, audit and prune old rules, turn on plan mode, try a task in a
  throwaway worktree, and set spending caps.
- **Project context (R12, removed).** Aurora used to load an
  `.agentic_context` folder itself. That now happens through the
  `agentic_context_mcp` MCP server, whose tools (`read_rules`, `list_index`,
  `read_doc`, `write_memory`, `stats`) go through the normal approval prompt.
  Configure it under `mcp_servers:` in `config.yaml`.
- **The terminal UI.** Esc is the one control key, pressed twice to confirm
  cancelling, leaving a mode or quitting. No single key quits by accident.
  Cancelling works even while the server is still reading the prompt. In
  the classic REPL the cancel key is Ctrl+C instead; the mechanism is the
  same.
- **Working while a turn runs (R245, R246, R253, R254, R348 to R351).** A
  running turn never locks the session. The model can run a long command in
  the background and check on it later. You can run a `!` command, a
  read-only `/command` or `/model`, or ask a side question, all on a second
  worker. That worker never writes the session history, which keeps a single
  writer. A side question is answered from a snapshot without tools, and says
  so; a `/model` switch waits until the turn ends. Nothing on the second
  worker may skip a check the main path makes, including the secret scan.
  You can also queue messages, send a line into the running turn, or type
  `stop` to cancel it.
- **Sessions and storage (R229 to R296).** Session records, checkpoints in a
  shadow git repo, atomic writes, compaction and history.
- **Resilience and performance (R246 to R298, R346, R393).** Every backend
  check has a time limit. When a backend can't be reached, Aurora explains
  why and keeps working with the others, so it works away from the home
  network with remote providers. Background work is bounded. The product
  contains nothing personal and assumes nothing about the machine it runs on.
- **Aurora Lite, the web version (R353 to R440).** `aurora --web` serves the
  same engine as a chat page: chats with folders and downloads, Markdown
  notes, files, voice, a model menu with a YAML editor, and light and dark
  themes that work on a phone. It needs a bearer token and asks
  before each write or command until auto-approve is turned on (R474). Front ends reach the engine only through
  `aurora/service.py` (R394).

## Rules for this project

- The repo is the source of truth. Aurora runs on macOS and Linux and is
  synced with git.
- The README, the requirements and the technical docs must match what the
  code does. Ship doc updates in the same commit as the change.

## Architecture

[`ARCHITECTURE.md`](ARCHITECTURE.md) covers the boundary between the engine
and the front ends, the module map and the data flow.
