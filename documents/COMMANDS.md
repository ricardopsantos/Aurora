# Aurora commands and daily use

The full command reference behind the quick list in the [README](../README.md).

## Daily use

| Input | Action |
|---|---|
| plain text (Ctrl+J, `\n` or `\br` for a new line) | Talk to the model. Writes and commands ask first, in a menu you answer with the arrow keys or a number: Yes / Allow for this session / Always allow (the prompt shows the rule it saves) / No / Stop / Comment (steer the model instead). |
| `/…` + Tab | Complete a slash command (built-ins and skills). Prefix matches come first, then names that contain the typed text (`/ert` finds `qwertyuio`). |
| `!` (on an empty prompt) | Bash mode. `>` becomes `$` and every Enter runs a shell command locally, with no model involved, until you press Esc or Backspace on an empty line. |
| `/model` | Arrow-key menu of OpenRouter ($) and local (free) models. The current one is marked `✔` and selected. Opening it refreshes OpenRouter prices in the background, at most once a day and only when the network is up; rows update in place. Your choice is remembered for the next start. If the model needs a key you haven't stored, Aurora asks; leave it blank to keep the previous model. In the TUI, Esc also closes this menu without a change. Every other menu needs an explicit pick. |
| `/model` → `+ add a model…` | The last row of the picker. Asks for an OpenRouter URL or `org/model` id and runs `/model add`. |
| `/model add <url>` | Add an OpenRouter model by its page URL or `org/model` id. Aurora adds it to `config.yaml`, fetches its context size, price and description from the OpenRouter catalog, asks for the key if it's missing, and switches to it. |
| `/model remove <name>` | Remove a configured model, by URL or exact name (`rm` works too). Removing the current one switches to the first remaining model that has a key. |
| `/status` | Backend health. For the local server it shows the model actually loaded and its context size. |
| `/ask <text>` | While a turn is running, ask a quick side question. It's answered at once with one call without tools, and isn't added to the history. When idle, it's an ordinary prompt. |
| `/cost` | Turns, tokens and estimated $ per model across every session on this machine. It reads the session logs, so it covers old sessions too. Treat it as an upper bound, not an invoice. You can also click the `$…` price on the status bar. For one session, use `/context <id>`. |
| `/context [all] [<id>]` | The cost tree: this session as turns, each with its thinking, prompt and completion tokens, tool calls and $, plus approvals, `/compact` folds and model switches where they happened. Shows the last 20 turns unless you add `all`; give a session id to read a past one. A session that used more than one model also gets the per-model breakdown from `/cost`. You can also click the `ctx` gauge on the status bar. |
| `/cache on\|off` | Prompt caching (on by default, saved). Marks the system prompt as cacheable so the bootstrap preamble isn't billed again on every tool step. Used for remote models; the local model is left alone because llama.cpp caches its own prefix. `/cost` shows the hits. |
| `/autocompact on\|off\|<pct>\|keep=<tokens>` | On by default, saved. Folds older history into a summary once the context passes the threshold. It checks between rounds of a running turn and at the end of each turn, and keeps recent turns as they are. (`/compact` folds everything, whenever you ask.) A number sets the threshold percent and `keep=<tokens>` sets how much recent history stays; both are saved. Aurora refuses out-of-range values (`0` would fold every turn; over `100` would never fold) and corrects a bad value in `config.yaml` with a warning. |
| `/fallback on\|off` | Off by default, saved. If a provider fails hard, Aurora retries the same turn on the next configured model that has a key, in the same order as the picker. |
| `/auto-approve on\|off` | Run tool calls without asking. The denylist still blocks. Works mid-turn. |
| `/thinking` | Show or hide the live reasoning stream. In the TUI you can also click any "thought for Ns" row to open or close it. |
| `/markdown` | Switch between formatted Markdown and raw text. |
| `/redact on\|off` | Secret detection in prompts and tool output (on by default, saved). A key, token or `.env` credential makes Aurora ask: keep it, redact it to `<secret>`, always allow it (never flagged again), or stop. |
| `/redact allowlist [clear]` | Show how many false positives you've allowed, or clear them. They're saved as hashes, never as the values. |
| `/multiline` | Multiline mode: Enter adds a new line and Alt+Enter sends. Saved to the config. |
| `?` (empty prompt + Enter) | Open the help menu, same as `/help`. It scrolls; Esc closes it. |
| Esc | The control key. See [Esc, pressed twice](#esc-pressed-twice) below. |
| Ctrl+C | In the TUI it clears the input line and never quits; to cancel a busy turn press Esc twice. In the classic REPL (`--classic`) it interrupts a busy turn, and at an idle prompt it just redraws. |
| `/compact` · `/clear` | Summarize and continue · start fresh. |
| `/reset` | Full reset: clears the history and system prompt, then offers to run `/bootstrap` again. |
| `/copy [N]` | Copy the Nth-last response to the clipboard. Works over SSH (OSC 52). |
| `/copy-last` | Copy the whole last turn: the prompt, the thinking and every reply in order, with a marker for each tool call (tool output left out). Also in the status bar's `copy` picker as "copy last turn: prompt + replies". |
| `/copy-response` | Copy only the model's final reply from the last turn, skipping a trailing message that only calls tools. Also "copy last turn: answer only" in the `copy` picker. |
| `/copy-turn` | Copy the last turn with everything: prompt, thinking, every reply, and each tool's command and full output. Also "copy last turn: everything" in the `copy` picker. |
| `/copy-all` | Copy the whole session: questions, answers, and each tool's command and output (the first 4000 characters of each, with a note for the rest; no thinking). Also "copy whole session" in the `copy` picker. |
| `/rewind [id]` | List the checkpoints taken before each approved write, edit or command, and restore one. A restore is checkpointed too, so you can undo it. |
| `/undo` | Revert only the last change instead of the whole tree. It always shows the diff and names the files before asking, so you can catch the wrong file before you confirm. |
| `/diff` | Show what the last turn changed, compared with its checkpoint. |
| `/commit [message]` | Stage, have the current model draft a commit message from the staged diff, show it, and commit when you approve. If nothing is staged it lists the changes and asks before `git add -A`. The menu offers Yes / Edit the message / Cancel; Cancel leaves the changes staged. Pass a message to skip the draft. Works on the project's real `.git`, never on the shadow repo `/rewind` uses. |
| `/nano <file>` | Open a text file in the built-in editor: `.txt`, `.md`, `.json`, `.yml`, `.yaml`, `.xml` or `.sh`, up to 1 MB. TUI only. |
| `/allowlist` | Review the saved allowlist and this session's "allow for this session" rules. |
| `/allowlist audit` · `/allowlist prune` | List rules that are duplicates, old single-word rules, or broader than today's rules allow; `prune` removes them after you confirm. |
| `/plan on\|off` | Plan mode, for this session only. Reads still run; anything that would ask is refused with "[plan mode — not run]", so the model writes a plan first. |
| `/budget [<usd>\|off]` · `/budget day <usd>\|off` | Spending caps for this session and for each day (the daily one is saved). Warns at 80% and stops the round at 100%. |
| `/try <task>` | Run the task in a throwaway git worktree (a copy of HEAD plus your uncommitted changes), then apply, discard or keep its diff. |
| `/denylist` | Review the tool calls that are always denied (set with "Always DENY" at the approval prompt). They are never asked again and no allow rule overrides them. |
| `/resume` · `/export [id]` | Pick a past session · save the current session as Markdown, or a past one by its id or a prefix (no `/resume` needed). |
| `/search <text>` | Search every session log on this machine, ignoring case. One hit per session, newest first; pick a number to resume it. |
| `/skills` · `/name args` | List or run skills (from `skills/` or `AURORA_HOME/skills/`). |
| `/extensions` | List the loaded extension tools (bundled and your own in `AURORA_HOME/extensions/`) and how to add one. The startup banner shows the count. |
| `/extensions new <name>` | Create a SPEC/RUNNERS template at `AURORA_HOME/extensions/<name>.py`. Fill in the tool function and restart Aurora to load it. |
| `/bootstrap` | Run your saved bootstrap prompt as the first turn. Also `set [file\|url] [project]`, `show` and `clear`. The global prompt is `AURORA_HOME/bootstrap.md`; a project's `.aurora/bootstrap.md` takes precedence. `set` with a URL downloads and caches it and remembers the source. At startup Aurora asks yes or no for a local prompt, or run cached / download again / skip for one from a URL. |
| `/help` · `/quit` (or `/exit`) | Help · quit at once. A typed command can act immediately; a key never does. |
| `aurora --debug` | Tint the chat area and status bar in two shades of red, so their edges are easy to see while you work on the layout. |

The screen has three fixed areas: the scrolling chat, the input line (where
questions and menus also appear), and a two-line status bar.

Line 1 of the status bar shows:

- the model (click it to open `/model`);
- the context used, the limit and the percentage, with a live
  `(+~N draft)` estimate for the text you're typing (click the `ctx` gauge
  for the `/context` cost tree);
- the average generation speed (`NN t/s`), once Aurora has measured at least
  a second of streaming;
- the mode, `prompt mode` or `bash mode`;
- a `copy` button. It opens a picker with the last turn (prompt, thinking,
  response), the whole session (questions and answers, no thinking), the
  session id, and the current selection when there is one.

Line 2 shows key hints you can click: `/ commands · ! bash · \n / \br newline
· Ctrl+J newline · ? Help · Esc cancel/clear/exit` in prompt mode, with
`! bash` replaced by `> prompt` in bash mode. A live status (thinking…,
waiting for an answer, a copy notice) takes its place while it lasts.

Output is coloured and Markdown streams as it renders. Set `NO_COLOR=1` to
turn colour off.

## Esc, pressed twice

Esc follows one rule everywhere in the TUI: press it twice within 2 seconds
to open a Yes/No question. The first press arms the action and may show a
hint on the status bar. The second opens the question, and nothing happens
until you pick an answer. If you wait longer than 2 seconds or do something
else, the first press is forgotten, so a stray Esc minutes later always
counts as a new first press.

| State | 1st Esc | 2nd Esc (within 2s) |
|---|---|---|
| Busy (thinking or working) | Arms; the status bar shows `Esc again to ask!` | Opens "Cancel this?". `Yes, cancel` stops the request; `No, keep going` closes the question. |
| Bash mode (`$` prompt) | Arms without a hint (or click the `> prompt` hint to leave in one step) | Opens "Leave bash mode?". `Yes` returns to the `>` prompt; `No` stays. |
| Idle, empty prompt | Arms without a hint | Opens "Quit Aurora?". `Yes` quits; `No` stays. Typing `y` and Enter at the status-bar question also works. |

Every question uses the same menu as the rest of the app: ↑/↓ and Enter, or
a number key to jump to an option.

Two cases don't follow this rule, on purpose:

- While a menu or approval question is open (pickers, `/model`, secret
  redaction, approvals), Esc does nothing. You have to pick an option.
- On an input line with text in it, one Esc clears the text. Retyping is
  easy; cancelling a request, leaving a mode or quitting is not.

## Typing while the model works

- Enter queues the line as the next prompt, and a note says so.
- Ctrl+Enter or Ctrl+Space sends the line into the running turn; the model
  reads it with the next tool result. Ctrl+Space works in every terminal.
  Ctrl+Enter works only in terminals that report it (kitty, WezTerm, iTerm2
  or xterm with modified-key reporting); the rest send a plain Enter. A line
  the model hasn't read when the turn ends becomes the next prompt. Pressing
  Esc twice drops it.
- Ctrl+Enter or Ctrl+Space on an empty line acts on everything queued. Plain
  prompts go into the running turn and safe `/commands` and `!` lines run
  alongside it. If the queue holds a command that rewrites the session
  (`/clear`, `/compact` and similar), the turn is cancelled and the whole
  queue runs next, in order.
- Typing `stop` or `/stop` cancels the running turn instead of queueing.
- Commands that only switch a setting (`/auto-approve`, `/plan`, `/budget`,
  `/thinking`, `/markdown`, `/fallback`, `/cache`, `/redact`,
  `/autocompact`, `/multiline`, with `on|off`) take effect at once. `/clear`,
  `/compact`, `/rewind` and `/undo` still wait, because they rewrite the
  history the turn is using.
- The approval menu has "Yes, and turn auto-approve ON" for when you're
  already being asked.
