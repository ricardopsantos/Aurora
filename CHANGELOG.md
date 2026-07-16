# Changelog

Aurora's version is `1.0.<commit-count>`, pinned at the point each release
was published — check `aurora --man` or `python3 -c "import aurora;
print(aurora.__version__)"` for what you're actually running. For the full
numbered requirements record, see `AURORA.md`.

## Unreleased

### Added
- `/model add <openrouter-url-or-id>` (R80): paste an OpenRouter model page
  URL and Aurora appends the model to config.yaml, fetches its context
  size/pricing/description from the OpenRouter catalog, prompts for the key
  if it isn't stored yet, and switches to it. OpenRouter-only for now. A
  model the catalog doesn't list is refused at the add ("not found on
  OpenRouter"), not discovered broken on the first send; an unreachable
  catalog adds it unverified with a warning.
- `/model remove <url-or-name>` (R81): drop a configured model (any
  provider) from config.yaml; removing the current one falls back to the
  first remaining model with a usable key.
- Full-screen TUI status bar now shows live input/output token counts during
  a turn and a final token tag after the response.
- TUI now persists full tracebacks of uncaught event-loop exceptions to
  `$AURORA_HOME/tui_crash.log` for easier debugging behind the alternate screen.
- `Frontend.on_usage()` callback so providers can report per-request token usage.

### Fixed
- TUI status-bar token-tag crash (`TypeError: object of type 'int' has no
  len()`).
- `tests/test_rewind.py` import failure — `tests/` is now an importable package
  via `tests/__init__.py`.
- Select-menu and challenge-prompt layout edge cases (narrow-terminal clipping
  and input-line height glitches).
- The first Esc of the double-tap gesture now shows its "Esc again to …"
  status-bar hint (R78a — documented since R62, never rendered).
- A provider error during the malformed-tool-call retry no longer leaves the
  corrective nudge in history as a stray user message (R78b).
- `/redact` appears in `/`-autocomplete again (R78c).
- `read_file` reads only the returned head of a file instead of slurping the
  whole file into memory first (R78d).
- Drag-select in the TUI copied lines offset by the top pad on a short
  transcript (R79a).
- A write/edit approval aimed at a binary or unreadable file no longer
  crashes the turn building its diff preview (R79b).
- One garbled SSE line from a provider no longer kills the whole stream
  (R79c).
- `web_fetch` streams with a 2MB cap instead of downloading unbounded
  bodies into memory (R79d); `/resume`'s session listing streams logs
  instead of reading each one whole (R79e).

- Race fixes (R82): a status-bar endpoint probe can no longer redirect an
  in-flight request's retries; the per-endpoint HTTP client pool is
  lock-guarded; Happy-Eyeballs winner selection is atomic (no leaked
  socket when both address families connect near-simultaneously); sending
  with no model configured notifies instead of erroring through a blank
  provider.

### Changed
- Draft token estimate in the TUI status bar is now shown inline with context
  usage.
- Docs reconciled with the code (R78f): `/max` marked removed, Esc-in-menu
  behavior corrected, stale module/test listings refreshed, a corrupted
  AURORA.md heading restored.

## 1.0 — first stable release (2026-07-15)

The first versioned release. Aurora is a micro terminal coding agent: a
tool loop (read/write/edit/run/search) with an approval gate in front of
every write or command, running against a local llama.cpp server or
OpenRouter.

**Highlights:**
- Full-screen TUI with a pinned prompt, live status bar (model, context
  used, cost), and a classic inline REPL fallback for pipes/CI.
- Steer instead of deny-and-redo: at any approval prompt, drop a comment
  and Aurora folds it into the same request instead of a flat no.
- Every prompt and tool output scanned for secrets before it's sent or
  logged — redact, allow, or stop.
- Sessions are durably logged and resumable; checkpoints let you
  `/rewind` any approved change.
- One model picker for local (free) and OpenRouter (paid, with live
  pricing) models, click-to-open from the status bar.

**Removed this release:** the Anthropic provider (OpenRouter-compatible
models only, for now), the `/max-wait` time-based challenge, and the
startup logo.
