"""`aurora --man` — a man-page-style manual, coloured like llama-pick's."""

from .colors import BOLD, CYAN, DIM, GREEN, RESET, YELLOW


def man_page() -> str:
    B, C, D, G, Y, R = BOLD, CYAN, DIM, GREEN, YELLOW, RESET
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
        {G}s{R} stop the whole turn
        {G}c{R} [text] don't run — steer the model with your text instead
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

{B}COMMANDS (inside the REPL){R}
    {C}/model{R}        Arrow-key menu: OpenRouter ($) · local
                  loaded (free) · local library (free, ~1-2 min load,
                  confirms global eviction). Current model marked {G}✔{R} and
                  pre-selected. An entry needing a key you don't have shows
                  {D}(no key set){R} — picking it offers to enter/store it
                  right there instead of failing later. Leaving the prompt
                  blank (empty or whitespace only) skips the switch entirely
                  — you stay on whichever model was active before. TUI only:
                  Esc also cancels it with no change (every other menu
                  requires an explicit pick).
    {C}/model add{R} {G}url{R} Add an OpenRouter model by its page URL
                  (https://openrouter.ai/<org>/<model>) or bare org/model id:
                  validates it against the OpenRouter catalog, appends it to
                  config.yaml, fetches ctx/pricing/description, asks for the
                  key if missing, and switches to it. OpenRouter-only.
    {C}/model remove{R} {G}name{R} Remove a configured model (URL or exact name;
                  {C}rm{R} works too). Removing the current one falls back to
                  the first remaining model with a usable key.
    {C}/compact{R}      Summarize history with the current model and carry only
                  the summary (plain flatten is the fallback when the model
                  is unreachable).
    {C}/clear{R}        Start fresh (history only; system prompt kept).
    {C}/reset{R}        Full reset: clear history + system prompt, then
                  offer to re-run the bootstrap prompt.
    {C}/copy{R} [{G}N{R}]     Copy the Nth-last response (OSC52 — works over SSH).
    {C}/copy-last{R}    Copy last turn's RAW response, thinking included
                  (OSC52 — works over SSH). Also the "copy last" status-bar button.
    {C}/copy-all{R}     Copy the whole chat — questions + answers, no thinking —
                  (OSC52 — works over SSH). Also the "copy all" status-bar button.
    {C}/redact{R} {G}on|off{R} Secret detection in prompts + tool output (API keys,
                  tokens, GUIDs, .env credentials) — default ON, persisted.
                  A match challenges you: keep it, redact to {D}<secret>{R},
                  always allow it (allowlists it — never flagged again), or
                  stop. {C}run_command{R} arguments only ever get a notice, never
                  redacted (the command needs the real value to work).
    {C}/redact allowlist{R} [{G}clear{R}] Show how many false positives are
                  allowlisted, or clear them all (persisted).
    {C}/status{R}       Backend health: local shows the real loaded model +
                  context size from /props; a remote model shows key presence.
    {C}/cost{R} [{G}all{R}]     Per-model token + $ breakdown for this session, or
                  every session on this machine. Read straight from the
                  session logs, so it works on past sessions too. Prices
                  come from {Y}providers/remote_context_limits.json{R}; the
                  total is an UPPER bound (cached tokens bill cheaper).
    {C}/cache{R} {G}on|off{R}   Prompt caching (persisted). Marks the system prompt
                  as cacheable so the bootstrap preamble isn't re-billed on
                  every tool iteration of every turn. On by default for
                  remote models, off for the local one (llama.cpp keeps its
                  own prefix cache). {C}/cost{R} shows the cache hits.
    {C}/todo{R}         Show the model's current task list. The model writes it
                  itself with the {Y}todo_write{R} tool on multi-step work;
                  {C}/clear{R} resets it with the conversation.
    {C}/think{R}        Print the last turn's reasoning (thinking models).
    {C}/thinking{R}     Toggle live reasoning: dim stream vs "(thinking…)"
                  marker. Default from {Y}runtime.show_thinking{R} in config.
    {C}/markdown{R}     Toggle pretty rendering (bold/code/bullets) vs raw text.
    {C}/multiline{R}    Toggle multiline mode (same as {B}Alt+M{R}; persisted).
    {C}/allowlist{R}    Show the persistent approval allowlist.
    {C}/rewind{R} [{G}id{R}]   Restore the working tree to a checkpoint. One is
                  snapshotted (shadow git under AURORA_HOME) before every
                  approved write/edit/command, labelled with the causing
                  prompt. Restoring is undoable — the pre-rewind state is
                  checkpointed too. Gitignored files are never touched.
    {C}/commit{R} [{G}msg{R}]  Stage + commit the REAL project repo (not
                  {C}/rewind{R}'s shadow one). Nothing staged? shows what
                  {C}git add -A{R} would include and asks first. Drafts a
                  message from the diff (style-matched to recent commits)
                  unless you pass one; shows it before committing, with a
                  chance to edit or cancel.
    {C}/resume{R}       Pick a past session and continue it. On quit Aurora
                  prints the exact command to re-enter the same session.
    {C}/export{R}       Dump the conversation as markdown in the cwd.
    {C}/skills{R}       List skills; run one with {C}/name{R} {G}args{R}.
    {C}/bootstrap{R}    Run the saved bootstrap prompt as a user turn.
                  {C}set{R} [{G}file{R}{Y}|{R}{G}url{R}] [{G}project{R}] · {C}show{R} · {C}clear{R} [{G}project{R}].
                  Global {G}AURORA_HOME/bootstrap.md{R}; a project's
                  {G}.aurora/bootstrap.md{R} overrides. {C}set{R} with a URL
                  downloads and caches it, remembering the URL; when one
                  exists, startup offers to run it — a plain yes/no for a
                  local file/paste, or run-cached / re-download / skip for
                  a URL-sourced prompt.
    {C}/remember{R} [{G}all{R}{Y}|{R}{G}last{R} [{G}k{R}]] Save what's worth keeping from the
                  session into MEMORY, with a per-finding approval
                  challenge. Default (no argument, or {C}last{R}) is just
                  the last question/reply pair; {C}last{R} {G}k{R} the last {G}k{R}
                  pairs; {C}all{R} the whole session. No context protocol
                  folder detected? Saves flat into
                  {G}~/AURORA_PFCS/MEMORY/{R} instead (machine-wide, not
                  project-specific).
    {C}/agentic_report{R} {D}(only shown once a context protocol folder — a
                  KNOWLEDGE/SKILL.md + MEMORY/SKILL.md pair — is
                  detected){R} Choose {C}Stats{R} (runs the folder's
                  {G}scripts/stats.sh{R}) or {C}Index{R} (pretty-prints
                  KNOWLEDGE/INDEX.md and MEMORY/INDEX.md). Also the target
                  of the TUI status bar's "agentic report" link.
    {C}/help{R} {C}/quit{R}   Command summary · quit immediately.
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
                  one go.

{B}FILES{R}
    {C}AURORA_HOME{R} {D}(default ~/.aurora; set at install, marker ~/.aurora-path){R}
        {G}sessions/*.jsonl{R}   full event logs (turns, tools, approvals)
        {G}allowlist.yaml{R}     persisted "always" approvals
        {G}keys.enc{R}           Fernet-encrypted key store (opt-in fallback)
        {G}skills/{R}            user skills (also {G}<repo>/skills/{R})
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
    {Y}LLAMADESK_TOKEN{R}     Token for LlamaDesk model-library switches
    {Y}AURORA_HOME{R}         Override the data dir
    {Y}NO_COLOR{R}            Disable colours

{B}EXAMPLES{R}
    {C}aurora{R}                          {D}# start in the current project{R}
    {C}aurora --continue{R}               {D}# pick up yesterday's session{R}
    {C}aurora key set{R} LLAMA_API_KEY    {D}# store your local server's bearer key{R}
    {C}aurora key clear --all{R}          {D}# log out of every configured provider{R}
"""
