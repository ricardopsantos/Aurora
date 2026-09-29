# Aurora — Commands & Daily Use

Full command reference behind the [README](../README.md)'s quick-start list.

## Daily use

| Input | Action |
|---|---|
| plain text (Ctrl+J or `\n` / `\br` newline) | talk to the model; writes/commands ask approval via a numbered/arrow-key menu: Yes / Always allow (remember) / No / Stop / Comment — steer the model instead |
| `/…` + Tab | slash-command autocomplete (built-ins + skills) |
| `!` (empty prompt) | persistent bash mode: `>` becomes `$`, every Enter runs a shell command locally (no LLM) until you press Esc or Backspace on an empty line |
| `/model` | arrow-key menu: OpenRouter ($) · local loaded (free) — current model marked `✔` and pre-selected. Opening it re-pulls each OpenRouter model's listed price in the background (at most once a day, and only if the network is reachable), updating the rows in place if the answer lands while you're still choosing; the choice is remembered and restored on the next start. Picking an entry with no key stored prompts for one; leaving it blank skips the switch and keeps the previous model. TUI only: Esc also cancels it with no change — every other menu in Aurora requires an explicit pick |
| `/model add <url>` | add an OpenRouter model by its page URL (or bare `org/model` id): appends it to `config.yaml`, fetches ctx/pricing/description from the OpenRouter catalog, asks for the key if missing, and switches to it |
| `/model remove <name>` | remove a configured model (URL or exact name; `rm` works too) — removing the current one falls back to the first remaining model with a key |
| `/status` | backend health — local shows the real loaded model + context size |
| `/cost` | per-model breakdown of turns, tokens and estimated $ across EVERY session on this machine. Read straight from the session logs, so it works on old sessions too; an upper bound, not an invoice. Or tap the `$…` price on the status bar. For one session's breakdown, use `/context <id>` instead |
| `/context [all] [<id>]` | the **cost tree** — this session drawn as turns, each with its thinking/prompt/completion tokens, tool calls and $, with approvals, `/compact` folds and model switches in place. Last 20 turns by default; name a session id to read a past one; a session that spanned more than one model (a `/model` switch, or `/fallback`) also gets `/cost`'s per-model breakdown. Or tap the `ctx …` gauge on the status bar |
| `/cache on\|off` | prompt caching (default ON, persisted): marks the system prompt as cacheable so the bootstrap preamble isn't re-billed on every tool iteration of every turn. On for remote models, off for the local one (llama.cpp caches its own prefix); `/cost` shows the hits |
| `/autocompact on\|off\|<pct>\|keep=<tokens>` | (default ON, persisted) silently folds OLDER history once context usage crosses the threshold, checked between rounds of a running turn as well as at the end of one — recent turns stay raw; unlike `/compact`, which always folds everything on demand. A bare number sets the threshold percent and `keep=<tokens>` the size of the raw tail kept; both persist. Out-of-range values are refused (`0` would fold on every turn, `>100` would never fire while still reporting ON), and a bad value hand-edited into `config.yaml` is corrected with a warning rather than obeyed |
| `/fallback on\|off` | (default OFF, persisted) on a hard provider failure, retries the same turn against the next configured model with a usable key instead of failing outright — a silent `/model` switch, same order the picker walks |
| `/thinking` | toggle the live dim reasoning stream (TUI: click any "thought for Ns" row to expand/collapse it, any time — no command needed) |
| `/markdown` | toggle pretty rendering (bold/code/bullets) vs raw text |
| `/redact on\|off` | toggle secret detection in prompts + tool output (default ON, persisted) — a detected key/token/`.env` credential challenges you to keep it, redact it to `<secret>`, always allow it (allowlist — never flagged again), or stop |
| `/redact allowlist [clear]` | show how many confirmed false positives are allowlisted, or clear them all (persisted as hashes, never raw values) |
| `/multiline` | toggle multiline mode: `Enter` inserts a newline and `Alt+Enter` submits instead; persisted to config |
| **?** (empty prompt + Enter) | open the help menu (same as `/help`) — scrollable; Esc to close |
| **Esc** | the control key — see [Esc, the double-tap control key](#esc-the-double-tap-control-key) below |
| **Ctrl+C** | TUI: clear the input line (never quits) — cancelling a busy turn is Esc-Esc instead (see below). Classic REPL (`--classic`) only: interrupts a busy turn; at an idle prompt it just redraws |
| `/compact` · `/clear` | summarize-and-continue · start fresh |
| `/reset` | full reset: clear history + system prompt, then offer to re-run `/bootstrap` |
| `/copy [N]` | copy Nth-last response to clipboard, SSH-safe (OSC52) |
| `/copy-last` | copy the last turn IN FULL — prompt, thinking, and every reply in order with a marker per tool call (tool output excluded) — to clipboard, SSH-safe — also `copy last (full)` in the status bar's `copy` picker |
| `/copy-response` | copy just the model's final reply from the last turn — no prompt, no thinking, no tool markers; skips a trailing tool-call-only message — also `copy last (response)` in the `copy` picker |
| `/copy-all` | copy the whole session transcript (questions + answers, no thinking) to clipboard, SSH-safe — also `copy whole session transcript` in the status bar's `copy` picker |
| `/rewind [id]` | list checkpoints taken before every approved write/edit/command and restore one — restoring is itself checkpointed, so it can be undone |
| `/undo` | revert just the LAST mutation rather than the whole tree (`/rewind`'s narrower sibling). Always previews the diff and NAMES the affected paths before asking, so "that's not the file I meant" is catchable before confirming, not after |
| `/diff` | show what the last turn actually changed, against its pre-mutation checkpoint |
| `/commit [message]` | stage, draft a commit message from the staged diff with the current model, show it, and commit on approval. With nothing staged it lists the unstaged/untracked changes and asks before `git add -A`; the confirm menu offers Yes / Edit the message / Cancel (cancel leaves changes staged). Pass a message to skip the drafting step. Operates on the REAL project `.git`, never `/rewind`'s shadow repo |
| `/nano <file>` | open a text file in the built-in editor — `.txt`/`.md`/`.json`/`.yml`/`.yaml`/`.xml`/`.sh`, up to 1MB. TUI only |
| `/allowlist` | review the persistent approval allowlist |
| `/denylist` | review tool calls always denied by policy (set via the approval prompt's "Always DENY") — never asked again, no allowlist bypass |
| `/resume` · `/export [id]` | pick a past session · dump the current session as markdown, or a past one directly by id/prefix (no `/resume` needed) |
| `/search <text>` | case-insensitive search over every session log on this machine (not just the current one) — one hit per session, newest first; pick a number to `/resume` it |
| `/skills` · `/name args` | list / run skills (from `skills/` or `AURORA_HOME/skills/`) |
| `/extensions` | list loaded extension tools (bundled + your own `AURORA_HOME/extensions/`), plus how to add one — also shown as a count in the startup banner |
| `/extensions new <name>` | scaffold a SPEC/RUNNERS template file into `AURORA_HOME/extensions/<name>.py` — fill in the tool function, restart Aurora to load it |
| `/bootstrap` | run your saved bootstrap prompt as the first turn; `set [file\|url] [project]` / `show` / `clear`. Global `AURORA_HOME/bootstrap.md`, project `.aurora/bootstrap.md` overrides. `set` with a URL downloads and caches it, remembering the source; when one exists, startup offers a plain yes/no for a local file/paste, or run-cached / re-download / skip for a URL-sourced prompt |
| `/help` · `/quit` (or `/exit`) | help · quit immediately — a typed command may be instant, a key never is |
| `aurora --debug` | tint the TUI's chat area and status bar red (distinct shades) so their bounds are obvious while you're working on layout |

The screen is three fixed areas: a scrolling chat/transcript, an input line
(also where challenges/menus render), and a two-line status bar. Line 1 is
identity only (model — click it to open `/model` — / context used·limit·%,
with a live `(+~N draft)` estimate of the unsent text you're typing — click
the `ctx` gauge for the `/context` cost tree — / current mode: `prompt
mode` or `bash mode` / and a single `copy` button, which opens a picker:
last turn's raw record (prompt, thinking, response), the whole session
transcript (questions + answers, no thinking), the session id, and — only
when something is selected — the selection); line 2 shows
clickable key hints — `/ commands · ! bash · `\n` / `\br` newline · Ctrl+J
newline · ? Help · Esc cancel/clear/exit` in prompt mode, with `! bash`
swapped for `> prompt` in bash mode (same row otherwise) — replaced by
whichever transient status is live (thinking… / awaiting an answer / a copy
notice). Coloured output + streaming markdown rendering throughout; set
`NO_COLOR=1` to disable.

## Esc, the double-tap control key

Esc has one generic rule everywhere in the TUI: **it needs pressing twice,
within 2 seconds, to open an explicit arrow-key Yes/No question.** The first
press *arms* the action and shows a hint on the status bar; the second press
opens the question — nothing happens until you actually pick an option.
Waiting longer than 2s, or doing something else first, clears the arm — a
stray Esc minutes later is always treated as a fresh first press, never a
leftover confirm.

| State | 1st Esc | 2nd Esc (within 2s) |
|---|---|---|
| Busy (thinking/working) | arms — status bar shows `Esc again to ask!` | opens **"Cancel this?"** — `Yes, cancel` interrupts the request, `No, keep going` dismisses it |
| Bash mode (`$` prompt) | arms silently (no status-bar tip — or tap the `> prompt` hint instead for a one-step leave) | opens **"Leave bash mode?"** — `Yes` returns to the `>` prompt, `No` stays |
| Idle, empty prompt | arms silently (no status-bar tip) | opens **"Quit Aurora?"** — `Yes` quits, `No` stays (typing `y` + Enter at the status-bar question also still works) |

Every question is the same arrow-key menu used everywhere else in the app —
↑/↓ + Enter, or a number key to jump straight to an option.

Two cases are **not** part of this double-tap rule, on purpose:
- **A menu or approval challenge** (arrow-key pickers, `/model`, secret
  redaction, approvals) — Esc does nothing while one is open; you must pick
  an option explicitly (arrow keys + Enter, or a number key).
- **Clearing typed text** — a single Esc on a non-empty input line just
  clears it; retyping is trivial, unlike cancelling a request, leaving a
  mode, or quitting the app.
