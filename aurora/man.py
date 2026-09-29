"""`aurora --man` — a man-page-style manual, coloured like llama-pick's.

`COMMAND_MAN` is the single source for BOTH the full manual's "COMMANDS"
section and the in-REPL `/<cmd> help` (alias `/<cmd> man`) lookup — one
command's entry can't drift out of sync with the other because there is
only one copy of the text. Keep entries THOROUGH: this is the reference a
user reaches for when the one-line `COMMAND_INFO` blurb (autocomplete,
the `/help` summary) wasn't enough, not a repeat of that same blurb.
"""

from .colors import BOLD, CYAN, DIM, GREEN, RESET, YELLOW

# Display order for the manual's COMMANDS section. `/cmd help` doesn't need
# this (it looks up one key directly) — this only controls the full page.
COMMAND_ORDER = [
    "model", "compact", "clear", "reset", "copy", "copy-last", "copy-response",
    "copy-all",
    "redact", "status", "cost", "context", "cache", "autocompact",
    "fallback", "auto-approve", "thinking", "markdown", "multiline", "allowlist",
    "denylist", "rewind", "undo", "diff", "commit", "resume", "search",
    "sessions", "export", "skills", "extensions", "bootstrap",
    "nano", "help", "quit", "exit",
]


def _entries(B, C, D, G, Y, R):
    """Built as a function of the colour constants so every entry can use
    them — `man_page()` and `command_man()` both call this fresh (colours
    are resolved once at import time in `colors.py`, so this is cheap, not
    re-evaluated per call in any costly sense)."""
    return {
"model": f"""Switch models, or manage the configured list.

    {C}/model{R}              Arrow-key menu: OpenRouter ($) · local loaded
                       (free) · local library (free, ~1-2 min load, confirms
                       global eviction). The current model is marked {G}✔{R}
                       and pre-selected. An entry needing a key you don't
                       have shows {D}(no key set){R} — picking it offers to
                       enter/store it right there instead of failing later.
                       Leaving the prompt blank (empty or whitespace only)
                       skips the switch entirely — you stay on whichever
                       model was active before. TUI only: Esc also cancels
                       with no change (every other menu requires an
                       explicit pick).
    {C}/model add{R} {G}url{R}    Add an OpenRouter model by its page URL
                       ({D}https://openrouter.ai/<org>/<model>{R}) or a bare
                       {G}org/model{R} id. Validates it against the
                       OpenRouter catalog, appends it to {Y}config.yaml{R},
                       fetches ctx/pricing/description, asks for the key if
                       missing, and switches to it. OpenRouter-only.
    {C}/model remove{R} {G}name{R} Remove a configured model (URL or exact
                       name; {C}rm{R} works too). Removing the current one
                       falls back to the first remaining model with a
                       usable key.""",

"compact": f"""Summarize the conversation so far and continue with only the
    summary — frees context without starting over. Uses the CURRENT model
    to write the summary; if that model is unreachable, falls back to a
    plain flatten (a mechanical trim, not a real summary) so the turn
    doesn't just fail. Distinct from {C}/autocompact{R}, which does this
    silently in the background near the context limit — {C}/compact{R} is
    the manual, on-demand version, and always folds EVERYTHING, not just
    the oldest part.""",

"clear": f"""Start fresh: history is cleared, but the system prompt (and any
    bootstrap that set it) is kept. For a full reset including the system
    prompt, use {C}/reset{R} instead.""",

"reset": f"""Full reset: clears history AND the system prompt, then offers to
    re-run the saved {C}/bootstrap{R} prompt (declining leaves you with no
    project context at all, same as a brand-new install). Use {C}/clear{R}
    instead if you just want the conversation gone but the current system
    prompt kept.""",

"copy": f"""{C}/copy{R} [{G}N{R}]  Copy the Nth-last assistant response to the
    clipboard (default: the last one). Uses OSC52, so it works over SSH
    with no local clipboard access needed on the remote end. Copies the
    final answer only — no thinking, no tool output; see {C}/copy-last{R}
    for the whole turn including reasoning, or {C}/copy-response{R} for the
    last turn's final reply with the empty-message edge handled.""",

"copy-last": f"""Copy the LAST turn IN FULL — the prompt that started it,
    all of the reasoning/thinking, and EVERY reply the model made, in order,
    with a one-line marker per tool call — to the clipboard (OSC52,
    SSH-safe). R256: a turn that used tools is a chain (narrate, call,
    narrate, call, answer), and this used to copy only its final message,
    which on a long turn is usually a short wrap-up — the substantive reply
    was dropped. Tool OUTPUT is still left out; one result can be 60KB.
    This is the one place either the prompt or the thinking is ever
    copyable; {C}/copy{R} and {C}/copy-all{R} both leave thinking out. Want
    just the answer? {C}/copy-response{R}. Also reachable as
    "copy last (full)" in the status bar's {C}copy{R} picker.""",

"copy-response": f"""Copy just the model's FINAL reply from the last turn —
    no prompt, no thinking, no tool markers (OSC52, SSH-safe). The narrow
    counterpart to {C}/copy-last{R}, which takes the whole turn. Unlike
    {C}/copy{R} it skips a trailing tool-call-only message, so a turn stopped
    at the approval gate still copies the last thing the model actually
    said instead of an empty string, and it never reaches back into the
    previous turn to find one. Also reachable as "copy last (response)" in
    the status bar's {C}copy{R} picker.""",

"copy-all": f"""Copy the WHOLE chat — every question and answer, in order —
    to the clipboard (OSC52, SSH-safe). Thinking is never included. Also
    reachable as "copy whole session transcript" in the status bar's
    {C}copy{R} picker.""",

"redact": f"""Secret detection in prompts and tool output — API keys, bearer
    tokens, GUIDs, {D}.env{R}-style assignments, plus a high-entropy
    fallback for tokens with no recognizable shape. Default {G}ON{R},
    persisted.

    {C}/redact{R} {G}on|off{R}         Toggle the feature.
    {C}/redact allowlist{R}     Show how many confirmed false positives are
                         allowlisted (matched by hash, never the raw
                         value) — never flagged again once allowlisted.
    {C}/redact allowlist clear{R}  Clear the allowlist; every match gets
                         challenged again from here on.

    A match challenges you: {G}keep{R} it as-is, {G}redact{R} it to
    {D}<secret>{R} in history/the log, {G}always{R} allow it (adds it to the
    allowlist), or {G}stop{R} the turn. {C}run_command{R}/{C}wait_until{R}'s
    own command STRING only ever gets a notice, never redacted or blocked —
    the command needs its real argument to actually work, and blocking
    would duplicate the approval gate it already passed.""",

"status": f"""Backend health check for the CURRENT model. A local (llama.cpp)
    backend reports the real loaded model and its live context size (via
    {D}/props{R}); an Ollama backend ({D}type: ollama{R} in config.yaml)
    reports the same via its own {D}/api/show{R}; a remote (OpenRouter)
    backend reports whether a key is present, since there's no equivalent
    live probe for it.""",

"cost": f"""Per-model token + $ breakdown across EVERY session ever logged on
    this machine — not just this one. Reads straight from the session
    JSONL logs, so it works on old sessions too, including ones from a
    previous Aurora process. Prices come from
    {Y}providers/remote_context_limits.json{R}; the total is a deliberate
    UPPER bound (cached tokens bill cheaper, but the discount isn't
    reported uniformly across providers, so nothing is subtracted). Also
    prints this session's own accrued cost alongside the grand total, so
    the two reconcile instead of just disagreeing. For ONE session's own
    breakdown — turn by turn, not just a per-model summary — use
    {C}/context{R} instead.""",

"context": f"""{C}/context{R} [{G}all{R}{Y}|{R}{G}N{R}] [{G}id{R}]  The "cost tree": this
    session (or a past one, by id) drawn as turns, each with its
    thinking/prompt/completion token counts, tool calls, and $ — with
    approvals, {C}/compact{R} folds, and model switches shown in place at
    the point they happened. Shows the last 20 turns by default; {G}all{R}
    for every one, or a bare number {G}N{R} for the last N. A session that
    used more than one model (a {C}/model{R} switch mid-session, or a
    {C}/fallback{R} retry) also gets a {C}/cost{R}-style per-model
    breakdown up top. When rendering the LIVE session, also prints a
    linear "at this rate: ~N more turn(s) until context fills (~$X total
    by then)" projection, once there's an actual multi-turn growth trend
    to extrapolate from. Also reachable by tapping the status bar's
    {B}ctx{R} gauge.""",

"cache": f"""{G}on|off{R}, persisted. Marks the system prompt as cacheable so
    the bootstrap preamble isn't re-billed on every tool iteration of every
    turn — it's the one part of a request that's byte-identical for the
    whole session. On by default for remote (OpenRouter) models; off for
    the local one, since llama.cpp keeps its own prefix cache already.
    {C}/cost{R} shows the cache-hit savings when this is paying off.""",

"autocompact": f"""{G}on|off{R}, persisted, ON by default. Silently folds
    OLDER history (never the recent part) once context usage crosses 80% —
    checked between rounds of a running turn as well as at the end of one,
    so a long tool-heavy task folds and keeps going instead of dying on a
    full context mid-turn. Unlike {C}/compact{R} (which always folds
    EVERYTHING on demand, keeps nothing raw, and only runs when you ask),
    this is the quiet background version that only trims what it needs to.""",

"fallback": f"""{G}on|off{R}, persisted, OFF by default. When ON, a HARD
    provider failure — a network/API error, NOT a tool failure or a denied
    approval — retries the SAME turn against the next configured model
    with a usable key, walking {Y}config.yaml{R}'s {G}models:{R} list in
    the same order {C}/model{R}'s own picker does. A successful fallback
    silently switches the current model, exactly as if you'd picked it
    from {C}/model{R} yourself.""",

"auto-approve": f"""{G}on|off{R}, SESSION-ONLY — always starts back OFF on a
    fresh run, unlike {C}/fallback{R}/{C}/multiline{R} above. While ON,
    every tool call that would normally stop and ask (write/edit a file,
    run a shell command, …) runs unprompted instead, same as if you'd
    picked "Yes" at every approval gate yourself.
    Two things it does NOT bypass: a {C}/denylist{R} rule still blocks its
    call outright (deny always wins, checked before this), and a detected
    secret still stops and asks separately — this only skips the ASK for
    an otherwise-approvable call, never a standing refusal or the
    secret-detection challenge.
    The status bar shows a persistent {Y}⚠ auto-approve ON{R} tag for as
    long as it's on, precisely because it's easy to turn on for one long
    task and forget it's still on for the next, unrelated one.""",

"thinking": f"""Toggle the live reasoning stream: a dim, real-time stream of
    the model's thinking vs. just a static "(thinking…)" marker while it
    works. Default comes from {Y}runtime.show_thinking{R} in config. TUI
    only, independent of this toggle: click any "thought for Ns" row in the
    transcript to expand or collapse it in place, any time, no command
    needed.""",

"markdown": f"""Toggle pretty rendering (bold, inline code, bullet lists) vs.
    raw text. A fenced code block tagged with a recognized language
    ({G}python js ts bash go rust ruby{R} + common aliases) also gets basic
    keyword/string/number syntax highlighting; an untagged or unrecognized
    fence just renders dim, same as with this off.""",

"multiline": f"""Toggle multiline input mode (same as {B}Alt+M{R}; persisted).
    ON: {B}Enter{R} inserts a newline and {B}Alt+Enter{R} submits. OFF (the
    default): {B}Enter{R} submits immediately. Either way, typing {B}\\n{R}
    or {B}\\br{R} inside a prompt inserts a literal newline, and a pasted
    newline never submits.""",

"allowlist": f"""Show the persistent "always allow" approval rules — added
    via the {G}a{R} option at any approval prompt. How far a rule
    generalizes depends on the command: read-only ones ({C}find ls grep{R}
    …) match across any arguments; destructive ones ({C}dd rm mkfs shred
    sudo sh python curl{R} … and anything using a shell operator) match the
    EXACT command only, so allowing {C}rm -rf ./build{R} never allows
    {C}rm -rf /{R}. Everything else stores a two-token prefix (e.g.
    {C}git push{R}). Persisted in {Y}AURORA_HOME/allowlist.yaml{R}.""",

"denylist": f"""Show tool calls always denied by policy — added via the
    {G}d{R} ("always DENY") option at any approval prompt. A denylist match
    skips the approval prompt entirely: no question is asked, the call is
    refused outright, every time. Persisted in
    {Y}AURORA_HOME/denylist.yaml{R}.""",

"rewind": f"""{C}/rewind{R} [{G}id{R}]  Restore the WHOLE working tree to an
    earlier checkpoint — the coarse option; see {C}/undo{R} for reverting
    just the last single mutation instead. A checkpoint (a snapshot in a
    private shadow git repo under {Y}AURORA_HOME{R}, separate from the
    project's own {D}.git{R}) is taken before every approved write/edit/
    command, labelled with the prompt that caused it. With no {G}id{R},
    lists the recent checkpoints newest-first and asks which to restore.
    Restoring resets tracked files to that snapshot and deletes anything
    created since — but it is itself undoable: the pre-restore state gets
    its own checkpoint first, and the restore message shows the id to go
    back with. Gitignored/excluded files (venvs, build output, caches) are
    never touched either way.""",

"undo": f"""Revert just the LAST mutation — not the whole tree (that's
    {C}/rewind{R}). ALWAYS shows what it's about to revert — the affected
    file(s) AND the actual diff — before asking to confirm (default: No).
    Tracks the target file directly (whatever {C}write_file{R}/
    {C}edit_file{R}/{C}apply_patch{R} last touched), so it works no matter
    where that file lives — inside the project or anywhere else on disk.
    A {C}run_command{R}/{C}wait_until{R} mutation (no single unambiguous
    target file) falls back to whatever changed in the project tree
    itself. If nothing qualifies — the last action left no trace anywhere
    Aurora can see, or it was a genuine no-op — says "nothing to undo"
    rather than guessing; it will never revert something OLDER than the
    last mutation. For that, use {C}/rewind{R} and pick a checkpoint
    explicitly. Also reachable as the status bar's {C}undo{R} button, shown
    once the first mutation this session has happened.""",

"diff": f"""Show what the LAST TURN actually changed — diffs the working
    tree against the checkpoint taken right before that turn started, so
    you see exactly what the approved writes/edits/commands did, without
    re-reading the whole conversation. Covers brand-new files as real
    additions too (not silently blank). Operates on {C}/rewind{R}'s shadow
    checkpoint history, never the project's real {D}.git{R} — for that,
    see {C}/commit{R}.""",

"commit": f"""{C}/commit{R} [{G}message{R}]  Stage and commit to the REAL
    project repository — never {C}/rewind{R}'s private shadow one. Nothing
    staged yet? Shows what {C}git add -A{R} would include and asks first.
    With no message, drafts one from the diff (style-matched to the
    repo's own recent commit messages) and shows it before committing,
    with a chance to edit or cancel; pass a message yourself to skip the
    draft step entirely.""",

"resume": f"""Pick a past session from a list and continue it exactly where
    it left off — full history restored, not just a summary. On quit,
    Aurora always prints the exact command to re-enter whatever session
    you were just in, whether you used {C}/resume{R} to get there or not.""",

"search": f"""{C}/search{R} {G}text{R}  Case-insensitive substring search over
    EVERY session log on this machine, not just the current one — matches
    both prompts and tool output. Shows one hit per session (the newest
    session first). Pick a number afterward to jump straight into
    {C}/resume{R}-ing that session.""",

"sessions": f"""List every past session on this machine — id, last-active
    time, and a preview of its first real task — newest first, with the
    session you're in right now marked {C}(current){R}. Read-only: unlike
    {C}/resume{R} and {C}/search{R} it never prompts for a number to jump
    into one. Pass a count ({C}/sessions 50{R}) to see more than the
    default 20.""",

"export": f"""{C}/export{R} [{G}id{R}]  Dump a conversation as a markdown
    file in the working directory — questions, answers, and tool activity,
    formatted for reading outside Aurora (a PR description, a note to a
    teammate, etc). No arg: the CURRENT session. A full or partial session
    id (as shown by {C}/resume{R} or {C}/sessions{R}) exports that PAST
    session straight from its log — no need to {C}/resume{R} it first.""",

"skills": f"""List every installed skill (bundled + your own, from
    {Y}<repo>/skills/{R} or {Y}AURORA_HOME/skills/{R}). Run one directly as
    {C}/{R}{G}skill-name{R} {G}args{R} — skills show up in {C}/{R}
    autocomplete alongside the built-in commands.""",

"extensions": f"""{C}/extensions{R}  List loaded extension tools — bundled
    ones (an MCP client, configured via {Y}mcp_servers:{R} in
    {Y}config.yaml{R}, plus {G}lint_check{R}) and any of your own from
    {Y}AURORA_HOME/extensions/{R} — and how to add one.
    {C}/extensions new{R} {G}name{R}  Scaffold a SPEC/RUNNERS template file
    into {Y}AURORA_HOME/extensions/{R}{G}name{R}{Y}.py{R} — fill in the
    tool function yourself, then restart Aurora to load it.""",

"bootstrap": f"""Manage the saved bootstrap prompt — the ONLY way any
    project context (e.g. an {D}.agentic_context{R} protocol) enters a
    session; nothing is auto-detected or auto-injected.
    {C}/bootstrap{R}              Run the saved prompt right now, as a
                           normal user turn.
    {C}/bootstrap set{R} [{G}file{R}{Y}|{R}{G}url{R}] [{G}project{R}]  Save a
                           new one — from a local file, a pasted prompt, or
                           a URL (downloaded and cached, remembering the
                           source URL for later re-download). Add
                           {G}project{R} to save it into THIS project's own
                           {Y}.aurora/bootstrap.md{R} (overrides the global
                           one) instead of the global
                           {Y}AURORA_HOME/bootstrap.md{R}.
    {C}/bootstrap show{R}         Print the currently saved prompt.
    {C}/bootstrap clear{R} [{G}project{R}]  Remove it (global, or just this
                           project's override).
    When a bootstrap prompt exists, Aurora offers to run it at startup —
    a plain yes/no for a local file or pasted prompt, or a choice of
    run-cached / re-download / skip for a URL-sourced one.""",

"nano": f"""{C}/nano{R} {G}file{R}  Open a text file
    ({G}.txt .md .json .yml .yaml .xml .sh{R}, up to 1MB) in Aurora's own
    built-in editor — TUI only, not available in the classic REPL. The
    editor takes over the chat area while open; the status bar swaps to
    save/close/save-and-close buttons in place of the usual model/context
    links for the duration. Clicking a matching filename anywhere in
    bash-mode output opens it the same way, with no need to type
    {C}/nano{R} yourself.""",

"help": f"""Print the full command + key summary (same content as the TUI's
    {B}?{R}-triggered scrollable help overlay on an empty prompt). For a
    single command's own full description instead of the one-line
    summary, use {C}/{R}{G}command{R} {C}help{R} (or {C}man{R} — they're
    synonyms).""",

"quit": f"""Quit Aurora immediately — no confirmation prompt. {C}/exit{R} is
    a full alias; either spelling works. In the TUI, pressing {B}Esc{R}
    twice on an empty, idle prompt asks the same "quit?" question through
    an explicit Yes/No menu instead — this command skips that and just
    quits.""",

"exit": f"""Alias of {C}/quit{R} — quits Aurora immediately, no confirmation.
    Both spellings do exactly the same thing; use whichever you reach for.""",
    }


def command_man(cmd: str) -> str | None:
    """The full `/<cmd> help`/`/<cmd> man` text for one command, or None if
    `cmd` isn't a recognized command at all. Same source `man_page()`'s
    COMMANDS section renders from — see this module's docstring."""
    B, C, D, G, Y, R = BOLD, CYAN, DIM, GREEN, YELLOW, RESET
    return _entries(B, C, D, G, Y, R).get(cmd)


def man_page() -> str:
    B, C, D, G, Y, R = BOLD, CYAN, DIM, GREEN, YELLOW, RESET
    entries = _entries(B, C, D, G, Y, R)

    def _block(cmd: str) -> str:
        # normalize rather than preserve each entry's own hand-typed
        # indentation — entries are authored assuming they're printed
        # standalone (`/cmd help`, at the left margin); nesting them under
        # a header here doubles whatever indent they already carry, which
        # reads as ragged, inconsistent wrapping. A flat 4-space indent for
        # every line (dropping each line's OWN leading whitespace first)
        # keeps sub-bullets like "/model add" readable without that.
        body = "\n".join(f"    {line.strip()}" if line.strip() else ""
                         for line in entries[cmd].splitlines())
        return f"    {C}/{cmd}{R}\n{body}\n"

    commands_section = "\n".join(_block(cmd) for cmd in COMMAND_ORDER
                                 if cmd in entries)
    return f"""
{B}NAME{R}
    aurora — micro terminal coding agent (OpenRouter / local llama.cpp)

{B}SYNOPSIS{R}
    {C}aurora{R} [{Y}--continue{R}] [{Y}--resume ID{R}] [{Y}--classic{R}] [{Y}--debug{R}] [{G}config.yaml{R}]
    {C}aurora{R} {Y}key set{R} [{G}ENV_VAR{R}]
    {C}aurora{R} {Y}key status{R} [{G}ENV_VAR{R}]
    {C}aurora{R} {Y}key clear{R} [{G}ENV_VAR{R}{Y}|--all{R}]
    {C}aurora{R} {Y}wipe{R}
    {C}aurora{R} {Y}--man{R} | {Y}--help{R}

{B}DESCRIPTION{R}
    A coding agent in the terminal: one model with a tool loop (read/write/
    edit files, run commands, grep, web search) behind an approval gate.
    Writes and commands show a diff and ask:
        {G}y{R} run once   {G}n{R} [reason] deny (reason shown to the model)
        {G}a{R} always-allow (persists to the allowlist)
        {G}d{R} always-DENY (persists to the denylist — never asked again)
        {G}s{R} stop the whole turn
        {G}c{R} [text] don't run — steer the model with your text instead
        {G}e{R} explain — the model describes what the call will do (a plain
              side question, no tools, not added to history), then the SAME
              approval prompt comes back so you can decide
    The iteration-cap prompt accepts {G}y{R} / {G}N{R} / {G}c{R} <guidance> the same way.

    The agent starts with NO project knowledge. Your saved {C}/bootstrap{R}
    prompt is what introduces any init ritual (e.g. an .agentic_context
    protocol) — nothing is detected or injected automatically.

    Every turn is appended to a JSONL session log; nothing is auto-deleted.

{B}ARGUMENTS{R}  {D}(all optional){R}
    {G}config.yaml{R}   Alternate config. {D}default: config.yaml in the repo root{R}

{B}FLAGS{R}
    {Y}--continue{R}    Resume the most recent session.
    {Y}--resume{R} {G}ID{R}    Resume a specific session ID (shown on quit).
    {Y}--classic{R}     Inline REPL instead of the full-screen TUI.
    {Y}--debug{R}       Tint the TUI areas to show their bounds: chat and
                 status bar red (distinct shades); the input line is left
                 untinted.
    {Y}--man{R}         This manual.

{B}COMMANDS (inside the REPL){R}  {D}— run `/<command> help` (or `man`) any
    time for the full entry below, without leaving the REPL{R}
{commands_section}
    {B}!{R}{G}cmd{R}          Classic REPL: run one bash command locally, no LLM.
                  TUI: {B}!{R} on an EMPTY prompt enters persistent bash mode
                  ({G}${R} prompt) — every Enter runs a command until you leave.

{B}KEYS{R}
    {B}Esc{R}           TUI only. A single Esc closes autocomplete, hides the
                  help overlay, or clears typed text. It does NOT resolve an
                  open challenge/approval menu — those require an explicit
                  pick (arrow keys + Enter, or a number key). Press Esc
                  TWICE within 2s to open an explicit Yes/No question:
                  cancel busy work · leave bash mode ({G}${R} prompt) · quit
                  (idle, empty prompt). Nothing happens until you pick an
                  option from that question.
    {B}Ctrl+C{R}        Clear the input line (classic REPL: interrupt).
    {B}Alt+M{R}         Toggle multiline mode: {B}Enter{R} inserts a newline,
                  {B}Alt+Enter{R} submits. Toggle is persisted to config.
    {B}Ctrl+J{R}        Insert a newline (pasted newlines never submit).
    {B}\\n{R} / {B}\\br{R}      Type either of these inside a prompt to insert a newline.
    {B}?{R}             TUI: type {B}?{R} on an empty prompt to open the
                  scrollable help overlay (same as {C}/help{R});
                  close with {B}Esc{R}. Classic REPL: type {B}?{R} and press
                  Enter.
    (Quit: {B}Esc{R} {B}Esc{R} on an empty prompt asks; {C}/quit{R} quits at once;
     classic REPL has no Esc binding — Ctrl+C interrupts, {C}/quit{R} quits.)
    {B}mouse drag{R}    Select chat text — stays highlighted on release and adds
                  a "copy selected" button to the status bar's top line; tap
                  it to copy (local: pbcopy/wl-copy/xclip; over SSH: OSC52).
                  The terminal's own selection is captured by the TUI; use
                  this instead. {C}/copy{R} grabs the whole last response in
                  one go. Dragging in the prompt line works the same way —
                  same button — and a {B}double-click{R} there selects the
                  whole draft.

{B}FILES{R}
    {C}AURORA_HOME{R} {D}(default ~/.aurora; set at install, marker ~/.aurora-path){R}
        {G}sessions/*.jsonl{R}   full event logs (turns, tools, approvals)
        {G}allowlist.yaml{R}     persisted "always allow" approvals. How far a
                          rule generalizes depends on the command: read-only
                          ({C}find ls grep{R} …) across any args; destructive
                          ({C}dd rm mkfs shred sudo sh python curl{R} …) across
                          NONE — exact command only, so allowing
                          {C}rm -rf ./build{R} never allows {C}rm -rf /{R}. Same for
                          anything with a shell operator. Others store a
                          two-token prefix ({C}git push{R}).
        {G}denylist.yaml{R}      persisted "always DENY" approvals
        {G}keys.enc{R}           Fernet-encrypted key store (opt-in fallback)
        {G}skills/{R}            user skills (also {G}<repo>/skills/{R})
        {G}extensions/{R}        user extensions ({C}/extensions{R} to list; SPEC + RUNNERS)
        {G}bootstrap.md{R}       default bootstrap prompt ({G}.aurora/bootstrap.md{R}
                           in a project overrides it)
        {G}state.yaml{R}         last-used model, restored on the next start
        {G}checkpoints/{R}       shadow git repos — {C}/rewind{R}'s pre-mutation
                           snapshots, one per project directory

    {C}aurora key status{R} [{G}ENV_VAR{R}]  Show where a key resolves from
                            (env var / OS keyring / encrypted file / not
                            set). No {G}ENV_VAR{R} = every key this
                            config.yaml uses.
    {C}aurora key clear{R} {G}ENV_VAR{R}   Remove a stored key (keyring + encrypted
                            file; can't unset an env var from here).
    {C}aurora key clear --all{R}    Same, for every key this config.yaml uses.
    {C}aurora wipe{R}               Delete {Y}AURORA_HOME{R} entirely — sessions,
                            allowlist, keys, bootstrap, state. Logs out of
                            every provider in one step. Requires typing
                            {G}yes{R} to confirm.

{B}ENVIRONMENT{R}
    {Y}OPENROUTER_API_KEY{R}  OpenRouter key {D}(else keyring → encrypted file → prompt){R}
    {Y}LLAMA_API_KEY{R}       Bearer key for your local llama-server endpoint
                     {D}(leave unset if your server needs no key){R}
    {Y}AURORA_HOME{R}         Override the data dir
    {Y}NO_COLOR{R}            Disable colours

{B}EXAMPLES{R}
    {C}aurora{R}                          {D}# start in the current project{R}
    {C}aurora --continue{R}               {D}# pick up yesterday's session{R}
    {C}aurora key set{R} LLAMA_API_KEY    {D}# store your local server's bearer key{R}
    {C}aurora key clear --all{R}          {D}# log out of every configured provider{R}
"""
