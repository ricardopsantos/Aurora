"""Terminal front end — a prompt_toolkit REPL implementing frontend.Frontend.

Owns ALL terminal I/O: streaming, footer (R13), keybindings (Shift+Enter /
Cmd+Enter newline, ? help, R18), Ctrl+C interrupt (R17), slash commands,
`!cmd` passthrough (R10), the /model picker. The engine is driven only
through its public methods.
"""

import getpass
import re
import subprocess
import sys
import threading

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from . import (
    approve,
    bootstrap,
    clipboard,
    extensions,
    gitcommit,
    keystore,
    mdrender,
    memory,
    rewind,
    skills,
    tokens,
    tools,
)
from . import session as sessions
from .colors import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    RED,
    RESET,
    YELLOW,
    colour_diff,
    dim,
)
from .engine import Engine
from .paths import aurora_home

HELP = f"""\
{BOLD}plain text{RESET}            talk to the model (paste is safe)
{CYAN}/<command> help{RESET}       full description of that one command (alias: `man`)
{BOLD}!{RESET} / {BOLD}!cmd{RESET}              bash: `!` on an empty line enters bash mode ($);
                      each Enter runs a shell command locally, no LLM
                      (Esc or empty backspace exits). Classic REPL: `!cmd`
{CYAN}/model{RESET}                model picker (OpenRouter $ · local free)
{CYAN}/model add <url>{RESET}      add an OpenRouter model (page URL or org/model id)
                      to config.yaml and switch to it
{CYAN}/model remove <name>{RESET}  remove a configured model (URL or exact name; `rm`
                      works too) — removing the current one falls back
{BOLD}Alt+M{RESET}               toggle multiline mode (Enter inserts a newline;
                      Alt+Enter submits)
{BOLD}\\n{RESET} / {BOLD}\\br{RESET}              type these in prose to insert a newline
{BOLD}Ctrl+J{RESET}                insert a newline
{BOLD}?{RESET}                    open this help menu (on an empty prompt)
{BOLD}Esc{RESET}                   TUI only — press twice within 2s to open a Yes/No
                      question: cancel busy work · leave bash mode · quit
                      (idle, empty prompt). A single Esc still clears input /
                      closes a menu / hides help. Classic REPL has no Esc
                      binding: Ctrl+C interrupts, /quit quits at once
{CYAN}/compact  /clear{RESET}      summarize-and-continue · start fresh
{CYAN}/reset{RESET}                clear history + system prompt; offers to re-run /bootstrap
{CYAN}/copy [N]{RESET}             copy Nth-last response to the clipboard (SSH-safe)
{CYAN}/copy-last{RESET}            copy last turn's RAW response, thinking included (SSH-safe)
{CYAN}/copy-all{RESET}             copy the whole chat, questions included, to the clipboard (SSH-safe)
{CYAN}/redact on|off{RESET}         secret-in-prompt/tool-output detection (default ON, persisted)
{CYAN}/status{RESET}               is the current model's backend up and ready?
{CYAN}/cost{RESET}                 per-model token + $ breakdown across EVERY session on this
                      machine — for one session's breakdown, {CYAN}/context <id>{RESET}
{CYAN}/cache on|off{RESET}          prompt caching: mark the system prompt cacheable so the
                      bootstrap preamble isn't re-billed every tool iteration
{CYAN}/autocompact on|off{RESET}   silently fold older history once context nears the limit
{CYAN}/fallback on|off{RESET}      retry a failed turn on the next configured model
                      (off by default); recent turns stay raw, /compact
                      still folds everything on demand
{CYAN}/thinking{RESET}            toggle live dim reasoning stream (TUI: click a
                      "thought for Ns" row to expand/collapse it any time)
{CYAN}/markdown{RESET}             toggle pretty rendering (bold/code/bullets) vs raw text
{CYAN}/allowlist{RESET}            show the persistent approval allowlist
{CYAN}/denylist{RESET}             show tool calls always denied by policy (never asked
                      again) — set from the approval prompt's "Always DENY"
{CYAN}/rewind [id]{RESET}          restore the tree to a pre-mutation checkpoint
{CYAN}/undo{RESET}                 revert just the LAST mutation, not the whole tree
{CYAN}/diff{RESET}                 show what the last turn actually changed
{CYAN}/resume  /export{RESET}      pick a past session · dump conversation as markdown
{CYAN}/search text{RESET}          search every session log for text, resume a hit
{CYAN}/skills  /name args{RESET}   list skills · run one
{CYAN}/extensions{RESET}           list loaded extension tools (bundled + your own
                      ~/.aurora/extensions/) and how to add one
{CYAN}/extensions new name{RESET}  scaffold a SPEC/RUNNERS template into
                      ~/.aurora/extensions/name.py
{CYAN}/bootstrap{RESET}            run the saved bootstrap prompt (set/show/clear to manage;
                      set accepts a local file OR a URL — startup offers
                      cached vs re-download for a URL-sourced prompt)
{CYAN}/remember [all|last [k]]{RESET}  save what's worth keeping from the session into
                      MEMORY — last exchange (default), last k, or all
{CYAN}/nano <file>{RESET}           TUI only — open a text file (.txt/.md/.json/.yml/
                      .yaml/.xml/.sh, up to 1MB) in the built-in editor;
                      clicking a matching filename in bash-mode output
                      opens it the same way
{CYAN}/help  /quit{RESET}          this help · quit immediately (Esc Esc asks first)"""


def help_text(has_agentic_context: bool = False) -> str:
    """HELP, plus the `/agentic_report` line ONLY when a context protocol
    folder was detected — that command doesn't exist as far as the user is
    concerned otherwise (see SlashCompleter, which hides it from
    autocomplete the same way)."""
    if not has_agentic_context:
        return HELP
    return HELP + (f"\n{CYAN}/agentic_report{RESET}      context folder "
                   "health: Stats (stats.sh) or a pretty-printed Index")


class TerminalFrontend:
    """The Frontend implementation (see frontend.py for the contract)."""

    def __init__(self, show_thinking: bool = False, render_md: bool = True):
        self.cancel_event = threading.Event()
        self.show_thinking = show_thinking   # live dim stream vs marker only
        self.render_md = render_md           # pretty markdown (display only)
        self.think_buffer = ""               # last turn's reasoning, for /copy-last
        self._think_marker_shown = False
        self._mdbuf = ""
        self._md = mdrender.LineRenderer()

    def begin_turn(self) -> None:
        self.think_buffer = ""
        self._think_marker_shown = False
        self._mdbuf = ""
        self._md = mdrender.LineRenderer()

    def end_turn(self) -> None:
        """Flush a trailing partial line of the markdown buffer."""
        if self._mdbuf:
            sys.stdout.write(self._md.render(self._mdbuf))
            sys.stdout.flush()
            self._mdbuf = ""

    # streaming
    def on_text(self, chunk: str) -> None:
        if self._think_marker_shown and not self.show_thinking:
            self._think_marker_shown = False
            sys.stdout.write("\n")           # separate answer from the marker
        if not self.render_md:
            sys.stdout.write(chunk)
        else:
            # render whole lines as they complete; hold the partial tail
            self._mdbuf += chunk
            while "\n" in self._mdbuf:
                line, self._mdbuf = self._mdbuf.split("\n", 1)
                sys.stdout.write(self._md.render(line) + "\n")
        sys.stdout.flush()

    def on_think(self, chunk: str) -> None:
        self.think_buffer += chunk
        if self.show_thinking:
            sys.stdout.write(f"{DIM}{chunk}{RESET}")
            sys.stdout.flush()
        elif not self._think_marker_shown:
            sys.stdout.write(dim("(thinking… — /copy-last includes it, "
                                 "or /thinking to stream it live)"))
            sys.stdout.flush()
            self._think_marker_shown = True

    def on_tool_start(self, name: str, args: dict) -> None:
        print(f"\n{CYAN}⚙ {name}{RESET}")
        for k, v in args.items():
            print(f"  {dim(k)}: {v}")

    def on_tool_result(self, name: str, output: str) -> None:
        head = output.strip().splitlines()
        shown = "\n".join(head[:6])
        more = f"\n  … +{len(head) - 6} lines" if len(head) > 6 else ""
        print(dim(f"  ↳ {shown}{more}"))

    def notify(self, message: str) -> None:
        print(f"\n{YELLOW}· {message}{RESET}")

    # prompts (called from the worker thread; main thread is join-waiting)
    def approve(self, tool: str, args: dict, diff: str) -> str:
        print(f"\n{MAGENTA}{BOLD}── approval: {tool} ─────────────────────{RESET}")
        if tool == "run_command":
            print(f"  {BOLD}$ {args.get('command', '')}{RESET}")
        elif tool == "wait_until":
            # bug fix: this used to fall through to the generic `path` branch
            # below, which is empty for wait_until — the command being
            # polled (and now, its optional `then` follow-up) never showed
            # at the approval prompt at all.
            print(f"  {BOLD}$ {args.get('command', '')}{RESET}")
            if args.get("then"):
                print(f"  {dim('then:')} {BOLD}{args['then']}{RESET}")
        elif tool.startswith("mcp_"):
            # R119: MCP tool args rarely have a "path"/"command" key the
            # generic branch below expects — show them as key: value instead,
            # same shape as on_tool_start's own tool-call rendering
            for k, v in args.items():
                print(f"  {dim(k)}: {v}")
        else:
            print(f"  {BOLD}{args.get('path', '')}{RESET}")
        if diff:
            print(colour_diff(diff))
        key = select("Approve?", [
            ("y", "Yes, run once"),
            ("a", "Always allow this (remember)"),
            ("n", "No, skip"),
            ("d", "Always DENY this (blocklist — never ask again)"),
            ("s", "Stop the agent"),
            ("c", "Comment — steer the model instead"),
            ("e", "Explain — describe what this will do, then ask again"),
        ])
        note = ""
        if key == "c":
            note = input(f"{YELLOW}guidance for the model:{RESET} ").strip()
        return key, note

    def _ask_keep_going(self, prompt: str, allow_silent: bool = False):
        """Backs ask_continue (iteration cap) — same menu, same
        guidance-comment path. `allow_silent` adds a "don't ask again this
        turn" option."""
        options = [
            ("y", "Yes, keep going"),
            ("n", "No, stop here"),
            ("c", "Comment — steer the model instead"),
        ]
        if allow_silent:
            options.insert(1, ("k", "Keep going — don't ask again this turn"))
        key = select(prompt, options)
        if key == "c":
            note = input(f"{YELLOW}guidance for the model:{RESET} ").strip()
            return True, note
        if key == "k":
            return "silent", ""
        return key == "y", ""

    def ask_continue(self, iterations: int):
        # R171/I2: `iterations` is `Turn.iterations` — it counts REQUEST
        # rounds (agent.py increments it once per `provider.turn` call),
        # including a round with zero tool calls (rare) and the R5
        # corrective-retry round, not "rounds that actually called a tool".
        # "request rounds" says what's really being counted.
        return self._ask_keep_going(
            f"{iterations} request rounds — continue?", allow_silent=True)

    def ask_secret(self, label: str) -> str:
        return getpass.getpass(label).strip()

    def secret_challenge(self, context: str, matches: list,
                         source_text: str = "") -> str:
        from . import secrets as secretscan
        if context == "prompt":
            where = "your prompt"
        elif context == "reply":
            where = "the assistant's reply"
        elif context.startswith("write:"):
            where = f"the `{context[len('write:'):]}` call about to run"
        else:
            where = f"`{context[5:]}` output"
        # "v" (feature request, 2026-07-27): toggles masking the token itself
        # in the lines below — a re-render-and-reask loop, same shape as the
        # approval gate's "e"xplain option, so the challenge stays open
        # rather than the toggle silently becoming a terminal answer.
        masked = False
        while True:
            print(f"\n{RED}{BOLD}── possible secret detected — {where} ────{RESET}")
            print(f"  {secretscan.preview(matches)}")
            if source_text:
                for line in secretscan.format_matches(source_text, matches,
                                                       mask=masked):
                    print(f"  {line}")
            choice = select("What should Aurora do?", [
                ("redact", "Replace with <secret> and continue"),
                ("keep", "Keep as-is and continue"),
                ("always", "Always allow this value — never flag it again"),
                ("v", ("Show" if masked else "Mask")
                     + " the token (safe for a shared screen)"),
                ("stop", "Stop"),
            ])
            if choice == "v":
                masked = not masked
                continue
            return choice

    def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Classic REPL ignores per-request usage; token accounting lives
        in the engine/session log."""

    def invalidate_status(self) -> None:
        """No-op: the classic REPL has no persistent status bar to redraw
        (R154). The engine-side gauge update it accompanies still happens —
        this is only the "now repaint it" half."""

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


def select(prompt: str, options: list[tuple[str, str]],
          default_index: int | None = None) -> str:
    """Numbered-menu choice. `options` is [(key, label), ...]; returns the
    chosen key. `default_index`, if given, is annotated "(current)" and is
    what a blank Enter accepts — opt-in, so approve/confirm-style callers that
    DON'T pass it keep blank-Enter re-prompting (an accidental Enter must
    never silently pick "yes" on an approval challenge). The TUI
    monkeypatches this name (same trick as `builtins.input`) to render an
    arrow-key-navigable menu instead, pre-highlighted on `default_index`."""
    print(f"\n{YELLOW}{prompt}{RESET}")
    for i, (_, label) in enumerate(options, 1):
        mark = " (current)" if default_index is not None and i - 1 == default_index else ""
        print(f"  {i}. {label}{mark}")
    valid_keys = {k.lower() for k, _ in options}
    while True:
        raw = input(f"{YELLOW}› {RESET}").strip()
        if not raw:
            if default_index is not None:
                return options[default_index][0]
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        if raw.lower() in valid_keys:
            return raw.lower()
        print(f"  {DIM}invalid choice — enter a number 1-{len(options)}{RESET}")


def confirm(prompt: str, default_yes: bool = True) -> bool:
    """A yes/no challenge as a numbered menu (never a bare text prompt). The
    default option is listed first so it is highlighted and Enter picks it —
    preserving the old '[Y/n]' / '[y/N]' default-on-empty behaviour."""
    opts = ([("y", "Yes"), ("n", "No")] if default_yes
            else [("n", "No"), ("y", "Yes")])
    return select(prompt, opts) == "y"


def _short(v, n: int = 60) -> str:
    s = str(v).replace("\n", "⏎")
    return s if len(s) <= n else s[:n] + "…"


def _expand_typed_newline(buf) -> bool:
    """If the text immediately before the cursor is \\n or \\br (a single
    backslash, not an escaped backslash), replace it with a real newline
    and return True. Otherwise leave the buffer untouched and return False."""
    text = buf.text
    pos = buf.cursor_position
    prefix = text[:pos]
    if prefix.endswith("\\n") and not prefix.endswith("\\\\n"):
        buf.cursor_position = pos - 2
        buf.delete(2)
        buf.insert_text("\n")
        return True
    if prefix.endswith("\\br") and not prefix.endswith("\\\\br"):
        buf.cursor_position = pos - 3
        buf.delete(3)
        buf.insert_text("\n")
        return True
    return False


def _expand_newlines(text: str) -> str:
    """Replace \\n and \\br tokens with real newlines.

    A doubled backslash disables expansion: `\\\\n` and `\\\\br` become the
    literal characters `\\n` and `\\br`.

    Only *backslash* sequences expand; forward-slash /n and /br are left
    literal so they never collide with slash commands."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        # two leading backslashes? keep the token literal
        if i + 1 < n and text[i + 1] == "\\":
            if i + 2 < n and text[i + 2] == "n":
                out.append("\\n")
                i += 3
                continue
            if i + 3 < n and text[i + 2] == "b" and text[i + 3] == "r":
                out.append("\\br")
                i += 4
                continue
        # plain token expansion
        if i + 1 < n and text[i + 1] == "n":
            out.append("\n")
            i += 2
            continue
        if i + 1 < n and text[i + 1] == "b" and i + 2 < n and text[i + 2] == "r":
            out.append("\n")
            i += 3
            continue
        # anything else is a literal backslash
        out.append("\\")
        i += 1
    return "".join(out)


_FOOTER_HINT = (" / commands · ! bash · \\n/\\br newline"
                " · Ctrl+J newline · ? Help · Ctrl+C interrupt")


# both live in tokens.py (engine-side, no UI deps — R90a); re-exported here
# because the TUI and the tests already reach for them as ui.<name>
estimate_tokens = tokens.estimate_tokens
fmt_token_count = tokens.fmt_token_count


def _footer(engine: Engine):
    """Two-line status bar under the prompt: model + context (always visible
    while typing) on top, key/command hints below."""
    def render():
        try:
            s = engine.context_stats()
        except Exception:
            return " aurora\n" + _FOOTER_HINT
        used = f"{s.used / 1000:.1f}k" if s.used >= 1000 else str(s.used)
        limit = f"{s.limit / 1000:.0f}k"
        cost = f" (${s.cost_usd:.2f})" if s.cost_known else ""
        warn = "  ⚠ context >80% — /compact?" if s.pct >= 80 else ""
        ml = " │ multiline" if engine.multiline else ""
        # R159: same rule as the TUI's `⤵N` — shown only once it has
        # happened. The classic REPL has no click target, so it is a plain
        # readout; `/compact` is typed here.
        folds = f" │ ⤵{s.compactions}" if s.compactions else ""
        draft = ""
        try:
            from prompt_toolkit.application import get_app
            text = get_app().current_buffer.text
            if text.strip():
                draft = f" (+~{estimate_tokens(text)} draft)"
        except Exception:
            pass
        return (f" {s.model}{cost} │ ctx {used}/{limit}{draft} ({s.pct:.0f}%)"
                f" │ session {s.session_id}{folds}{warn}{ml}\n"
                + _FOOTER_HINT)
    return render


def _prompt_message():
    """A dim full-width rule above the input — separates the prompt area
    from the chat scroll, à la Claude Code / Copilot."""
    import shutil
    width = shutil.get_terminal_size((80, 24)).columns
    return [("fg:ansibrightblack", "─" * width + "\n"),
            ("bold fg:ansicyan", "> ")]


def _send_turn(engine: Engine, fe: TerminalFrontend, text: str,
              is_bootstrap: bool = False) -> None:
    """The turn itself: clear cancel, begin/send/end, surface an error
    instead of raising through the caller. Shared by both front ends — the
    difference between them is only how the CALL is wrapped (see
    `_run_turn` vs. the TUI's `_worker`, R96f)."""
    fe.cancel_event.clear()
    fe.begin_turn()
    try:
        engine.send(text, fe, bootstrap=is_bootstrap)
    except BaseException as e:  # surface, don't kill the session
        fe.end_turn()
        print(f"\n{RED}✗ {e.__class__.__name__}: {e}{RESET}")
        print()
        return
    fe.end_turn()
    print()


def _run_turn(engine: Engine, fe: TerminalFrontend, text: str,
              is_bootstrap: bool = False) -> None:
    """Classic REPL only: run `_send_turn` in a worker so the MAIN thread can
    catch Ctrl+C (R17) — `input()`'s blocking read is what would otherwise
    eat the SIGINT.

    R96f: the TUI does NOT go through this. It used to (via this same
    function), paying for a thread plus a 10Hz join-poll on every turn for a
    KeyboardInterrupt handler that can never fire there: SIGINT is only ever
    delivered to the process's MAIN thread, and `_run_turn` was being called
    from the TUI's `_worker` thread, not it. prompt_toolkit also runs the
    terminal in raw mode, so ^C never becomes a signal in the TUI at all —
    cancellation there is `fe.cancel_event.set()`, wired through the Esc-Esc
    menu (`_resolve_cancel_menu`), entirely separate from this mechanism.
    """
    err: list[BaseException] = []

    def work():
        try:
            _send_turn(engine, fe, text, is_bootstrap)
        except BaseException as e:  # pragma: no cover — _send_turn doesn't raise
            err.append(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    while t.is_alive():
        try:
            t.join(0.1)
        except KeyboardInterrupt:
            fe.cancel_event.set()
    if err:
        raise err[0]


# ── /model picker ─────────────────────────────────────────────────────────
def _prompt_and_store_key(engine: Engine, env: str) -> None:
    """Offer to enter/store a missing key right after picking a model that
    needs one — instead of leaving the user with '(no key set)' and no way
    to fix it short of a separate `aurora key set` invocation. Same
    fetch-command-then-hidden-prompt flow as `aurora key set`.

    Uses `keystore._prompter`, NOT raw `getpass.getpass()`: the TUI routes
    secret prompts through `fe.ask_secret` (wired via `keystore.set_prompter`
    at startup) so they render through its own input line. Calling getpass
    directly reads from the real tty instead — invisible in the TUI's
    alternate screen, and it blocks the worker thread forever waiting for
    input nobody can see to give (this shipped as a real bug: /model looked
    like it hung on 'thinking' with no prompt ever shown)."""
    cmd = (engine.cfg.get("key_fetch") or {}).get(env)
    val = ""
    if cmd:
        print(f"fetch {env} by running:\n  {cmd}")
        if confirm("Run the fetch command?", default_yes=False):
            r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                              text=True, timeout=120)
            val = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
            if not val:
                print("fetch produced no output — falling back to manual entry")
    if not val:
        val = keystore._prompter(f"{env} (input hidden, empty to skip): ").strip()
    if not val:
        print(f"· skipped — set it later with: aurora key set {env}")
        return
    where = keystore.store_key(env, val)
    print(f"{GREEN}stored {env} in {where}{RESET}")


def _pick_model(engine: Engine, fe: TerminalFrontend) -> None:
    loaded = None

    from .providers.openai_compat import REMOTE_CONTEXT_LIMITS

    def _price(price_in: float | None, price_out: float | None) -> str:
        if price_in is None or price_out is None:
            return ""
        if price_in == 0 and price_out == 0:
            return "free"
        return f"${price_in:g}/${price_out:g} per M"

    def _info(ctx: int | None, size: int | None = None,
              price_in: float | None = None, price_out: float | None = None) -> str:
        parts = []
        if size:
            parts.append(f"{size / 1e9:.1f}GB")
        if ctx:
            parts.append(f"{ctx // 1000}k ctx")
        price = _price(price_in, price_out)
        if price:
            parts.append(price)
        return dim(" · ".join(parts)) if parts else ""

    entries = []       # (label, payload)
    current_index = 0  # which entry is the active model — pre-highlighted
    # identity (`is`) isn't reliable here: switch_model() stores whatever dict
    # it was handed, which is rarely the SAME object as the matching entry in
    # engine.list_models() (a fresh copy parsed from config.yaml) — compare by
    # the (provider, model) pair instead.
    cur_key = (engine.current.get("provider"), engine.current.get("model"))
    # feature request, 2026-07-27: last-known request latency per model,
    # cached from the session log — a live per-entry probe would add real
    # latency/complexity to opening the picker itself, so this is read-only
    # and can be stale or simply absent for a model not used recently.
    latencies = sessions.last_latency_by_model()
    # alphabetical picker: config models A→Z
    for m in sorted(engine.list_models(),
                    key=lambda m: str(m.get("model", "")).lower()):
        name = m.get("model", "")
        is_openrouter = name != "local" and "openrouter" in str(m.get("provider", ""))
        remote = REMOTE_CONTEXT_LIMITS.get(name, {}) if is_openrouter else {}
        # a known-$0 OpenRouter model (e.g. a ":free" variant) is genuinely
        # free, not "paid, happens to cost nothing" — tag it [free] like
        # local, not [$]. Only an UNKNOWN price defaults to assuming paid.
        known_free = (remote.get("price_in_per_mtok") == 0
                     and remote.get("price_out_per_mtok") == 0)
        paid = is_openrouter and not known_free
        tag = f"{YELLOW}[$]{RESET}" if paid else f"{GREEN}[free]{RESET}"
        if is_openrouter:
            info = _info(remote.get("context_size"),
                        price_in=remote.get("price_in_per_mtok"),
                        price_out=remote.get("price_out_per_mtok"))
        else:
            info = ""
        latency = latencies.get(name)
        if latency is not None:
            shown = f"{latency * 1000:.0f}ms" if latency < 1 else f"{latency:.1f}s"
            info = f"{info} · {shown}" if info else shown
        if name == "local":  # show what "local" actually is
            live = loaded
            if not live:
                # R170j: nonblocking — a direct provider.live_model_name()
                # call here is a live /props probe that can wait out its own
                # 4s timeout against a dead local server, freezing picker
                # construction before the menu even renders. Serves cache
                # (or None the first time) and refreshes in the background.
                provider = engine._provider_for(m)
                live = engine._live_model_name_nonblocking(provider)
            if live:
                name = f"local {DIM}→ {live}{RESET}"
        is_current = (m.get("provider"), m.get("model")) == cur_key
        mark = f"  {GREEN}{BOLD}✔{RESET}" if is_current else ""
        no_key = f"  {RED}(no key set){RESET}" if not engine.has_key(m.get("provider")) else ""
        if is_current:
            current_index = len(entries)
        entries.append((f"{name}{mark}  {tag} {info}{no_key}", m))

    options = [(str(i), label) for i, (label, _) in enumerate(entries, 1)]
    chosen = select("Select model", options, default_index=current_index)
    if chosen is None:   # TUI: menu dismissed (e.g. a second click on the
        return            # status bar's model name) — no change
    idx = int(chosen) - 1
    _, payload = entries[idx]

    pkey = payload.get("provider")
    if not engine.has_key(pkey):
        env = engine.cfg["providers"].get(pkey, {}).get("api_key_env")
        if env:
            _prompt_and_store_key(engine, env)
            engine.forget_key_check(pkey)   # pick up the freshly-stored key
            if not engine.has_key(pkey):
                # no key entered — stay on whatever model was active
                print(f"{YELLOW}· no key stored — keeping "
                      f"'{engine.current.get('model')}'{RESET}")
                return
    engine.switch_model(payload)
    print(f"{GREEN}→ {payload.get('model')}{RESET}")


# ── /model add (R80) ───────────────────────────────────────────────────────
_OPENROUTER_URL_RE = re.compile(r"^https?://openrouter\.ai/(?:models/)?", re.IGNORECASE)


def _parse_openrouter_model(arg: str) -> str | None:
    """An OpenRouter model page URL (https://openrouter.ai/<org>/<model>) or
    a bare '<org>/<model>' id → the model id, or None when it's neither."""
    model_id = _OPENROUTER_URL_RE.sub("", arg.strip()).strip("/")
    if not model_id or "/" not in model_id or any(c.isspace() for c in model_id):
        return None
    return model_id


def _add_model_cmd(engine: Engine, arg: str) -> None:
    """`/model add <url-or-id>` (R80): append the model to config.yaml under
    the `openrouter` provider and switch to it. If OPENROUTER_API_KEY isn't
    stored yet, offer to enter it first (same flow as picking a keyless
    entry in the picker)."""
    model_id = _parse_openrouter_model(arg)
    if model_id is None:
        print("· usage: /model add https://openrouter.ai/<org>/<model>  "
              "(or the bare <org>/<model> id)")
        return
    pcfg = engine.cfg["providers"].get("openrouter")
    if not pcfg:
        print("· no `openrouter` provider in config.yaml — add one first "
              "(see config.yaml.example)")
        return
    # validate against OpenRouter's catalog FIRST — a typo'd model id must
    # fail here, not on the first send. The same lookup supplies the
    # ctx/pricing/description for the footer gauge + $ badge (R71/R73).
    from .providers.openai_compat import (
        fetch_openrouter_model_info,
        save_remote_model_info,
    )
    info, catalog_ok = fetch_openrouter_model_info(model_id)
    if catalog_ok and info is None:
        print(f"{RED}✗ {model_id} not found on OpenRouter — check the "
              f"URL/id (https://openrouter.ai/models){RESET}")
        return
    env = pcfg.get("api_key_env")
    if env and not engine.has_key("openrouter"):
        _prompt_and_store_key(engine, env)
        engine.forget_key_check("openrouter")
    entry, created = engine.add_model(model_id)
    print(f"· added {model_id} to config.yaml" if created
          else f"· {model_id} is already configured")
    if info and any(info.get(k) for k in
                    ("context_size", "price_in_per_mtok", "price_out_per_mtok")):
        save_remote_model_info(model_id, info)
        ctx = info.get("context_size")
        pi, po = info.get("price_in_per_mtok"), info.get("price_out_per_mtok")
        bits = []
        if ctx:
            bits.append(f"ctx {int(ctx) // 1000}k")
        if pi is not None and po is not None:
            bits.append(f"${pi:g}/${po:g} per M (listed price)")
        print(dim(f"  {' · '.join(bits)}"))
    elif not catalog_ok:
        print(dim("  couldn't reach the OpenRouter catalog — model added "
                  "unverified; ctx/pricing unknown (add them to "
                  "remote_context_limits.json by hand)"))
    if engine.has_key("openrouter"):
        engine.switch_model(entry)
        print(f"{GREEN}→ {model_id}{RESET}")
    else:
        print(f"{YELLOW}· no key stored — added but not selected; set it "
              f"with: aurora key set {env}{RESET}")


def _remove_model_cmd(engine: Engine, arg: str) -> None:
    """`/model remove <url-or-name>` (R81): drop a model from config.yaml.
    Accepts the OpenRouter page URL or the exact configured model name (any
    provider — `local` included). Removing the current model falls back to
    the first remaining model with a usable key."""
    model_id = _OPENROUTER_URL_RE.sub("", arg.strip()).strip("/")
    if not model_id or any(c.isspace() for c in model_id):
        print("· usage: /model remove <https://openrouter.ai/<org>/<model> "
              "| model name>")
        return
    removed, new_current = engine.remove_model(model_id)
    if not removed:
        print(f"· {model_id} is not in config.yaml — /model lists what is")
        return
    print(f"· removed {model_id} from config.yaml"
          + (f" ({removed} entries)" if removed > 1 else ""))
    if new_current is None:
        return
    if new_current:
        print(f"{GREEN}→ {new_current.get('model')}{RESET}")
    else:
        print(f"{YELLOW}· no models left in config.yaml — add one with "
              f"/model add <url>{RESET}")


COMMAND_INFO = {
    "model":     "model picker — OpenRouter $ · local free · add/remove <url>",
    "status":    "is the current model's backend up and ready?",
    "thinking":  "toggle the live dim reasoning stream",
    "markdown":  "toggle pretty rendering vs raw text",
    "compact":   "summarize-and-continue (frees context)",
    "clear":     "start fresh — history cleared",
    "reset":     "clear history + system prompt; offers /bootstrap",
    "copy":      "copy Nth-last response to the clipboard",
    "copy-last": "copy last turn's RAW response (thinking included) to the clipboard",
    "copy-all":  "copy the whole chat (questions + answers) to the clipboard",
    "redact":    "secret detection on|off · allowlist [clear] (persisted)",
    "cost":      "per-model token + $ breakdown across every session on this machine",
    "context":   "cost tree — this session as turns, tokens, tools and $",
    "cache":     "prompt caching on|off — stops re-billing the system prompt",
    "autocompact": "silently fold older history near the context limit on|off",
    "fallback":  "retry a failed turn on the next configured model on|off (persisted)",
    "allowlist": "show the persistent approval allowlist",
    "denylist":  "show tool calls always denied by policy",
    "rewind":    "restore the working tree to a pre-mutation checkpoint",
    "undo":      "revert just the last mutation, not the whole tree (see /rewind)",
    "diff":      "show what the last turn actually changed (vs its pre-mutation checkpoint)",
    "commit":    "stage + draft a commit message from the diff + commit (optional message)",
    "resume":    "pick a past session to continue",
    "search":    "search every session log for text (add a number to resume that hit)",
    "export":    "dump this conversation as markdown",
    "skills":    "list installed skills",
    "extensions": "list loaded extension tools + how to add one",
    "bootstrap": "run the saved bootstrap prompt (set/show/clear)",
    "multiline": "toggle multiline mode: Enter newline, Alt+Enter submit (persisted)",
    "remember":  "save what's worth keeping from the session into MEMORY (last [k]|all)",
    "agentic_report": "context folder health: Stats (stats.sh) or a pretty-printed Index",
    "nano":      "open a text file (.txt/.md/.json/.yml/.yaml/.xml/.sh, up to 1MB) "
                 "in the built-in editor (TUI only)",
    "help":      "all commands and keys",
    "quit":      "quit aurora immediately (no confirmation; /exit works too)",
    "exit":      "quit aurora immediately (alias of /quit)",
}
COMMANDS = list(COMMAND_INFO)


class SlashCompleter(Completer):
    """Autocomplete for /commands (built-ins + installed skills). Only fires
    on a leading '/', so normal prose never pops a menu. Each entry carries
    a short description rendered beside the name in the menu."""

    def __init__(self, config_base: str | None):
        self.config_base = config_base
        # computed once per completer lifetime (= per session), not per
        # keystroke — find_context_root() walks the filesystem.
        # From the CWD, never config's _base_dir (R90c): the config lives in
        # the Aurora checkout, which has its own context folder, so keying on
        # it offered /agentic_report in every project regardless.
        self._has_agentic_context = memory.find_context_root(".") is not None
        self._skills_stamp: tuple | None = None
        self._skills: dict[str, str] = {}

    def _skill_entries(self) -> dict[str, str]:
        """name -> blurb for the installed skills, cached against the skills
        dirs' mtimes (R96a).

        This is on the per-keystroke path — the TUI wires the completer with
        `complete_while_typing=True`, and prompt_toolkit's default
        `get_completions_async` just iterates `get_completions` inline, so it
        runs on the event-loop thread. Walking the dirs and opening every
        skill there put blocking filesystem I/O directly into keystroke
        latency; `skills.dir_stamp()` costs two `stat()` calls and still
        catches a skill being added or removed.
        """
        stamp = skills.dir_stamp(self.config_base)
        if stamp != self._skills_stamp:
            self._skills = {
                name: (skills._blurb(path) or "installed skill")
                for name, path in skills.discover(self.config_base).items()}
            self._skills_stamp = stamp
        return self._skills

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        empty = text == ""   # explicit open (e.g. a status-bar click) with
        # nothing typed yet — list every command with the leading "/"
        # baked into the completion text itself, since there's no "/" in
        # the buffer to anchor a partial-prefix replace against.
        if not empty and (not text.startswith("/") or " " in text):
            return
        prefix = "" if empty else text[1:]
        entries = dict(COMMAND_INFO)
        if not self._has_agentic_context:
            # doesn't exist as far as the user is concerned otherwise (see
            # ui.help_text, which hides it from /help the same way)
            entries.pop("agentic_report", None)
        for name, blurb in self._skill_entries().items():
            entries.setdefault(name, blurb)
        for name, info in sorted(entries.items(), key=lambda kv: kv[0].lower()):
            if name.startswith(prefix):
                if empty:
                    yield Completion(f"/{name}", start_position=0,
                                     display=f"/{name}", display_meta=info)
                else:
                    yield Completion(name, start_position=-len(prefix),
                                     display_meta=info)


# ── /bootstrap ────────────────────────────────────────────────────────────
def _bootstrap_run_choice(url: str | None) -> str:
    """"run"/"download"/"skip" — a plain yes/no when the prompt is a local
    file/paste, or a 3-way choice when it's URL-sourced (we already have a
    cached copy from last time, so re-fetching on every startup shouldn't be
    silently assumed either way)."""
    if url:
        return select("Run the bootstrap prompt?", [
            ("run", "Run the cached version"),
            ("download", f"Re-download from {url} and run"),
            ("skip", "Skip"),
        ], default_index=0)
    return "run" if confirm("Run the bootstrap prompt?") else "skip"


def _run_bootstrap(engine: Engine, fe: TerminalFrontend, redownload: bool = False,
                   sync: bool = False) -> None:
    """`sync=True` (the TUI, R96f) calls `_send_turn` directly — it's already
    on a background thread with its own cancellation (Esc-Esc), so the
    classic REPL's Ctrl+C thread wrapper (`_run_turn`) would be pure
    overhead. `sync=False` (classic REPL, the default) is unchanged."""
    if redownload:
        print(dim("· re-downloading bootstrap prompt..."))

        def _confirm_refresh(old_text: str, new_text: str) -> bool:
            # R171/S3: this prompt is about to be sent as a tool-enabled
            # turn — show what actually changed before it's persisted (and
            # before any of it can run), rather than silently trusting
            # whatever came back over the wire.
            print(dim(f"· fetched content differs from the cached copy "
                      f"({len(old_text)} → {len(new_text)} chars):"))
            print(new_text[:2000] + ("\n…[truncated]" if len(new_text) > 2000
                                     else ""))
            return confirm("Use this fetched version?")

        try:
            refreshed = bootstrap.refresh_from_source(
                ".", confirm=_confirm_refresh)
        except Exception as e:
            print(f"· download failed: {e} — using cached version")
        else:
            if refreshed:
                print(dim(f"· updated → {refreshed[1]}"))
            else:
                print(dim("· keeping cached version"))
    text, source = bootstrap.load(".")
    if not text:
        print("· no bootstrap prompt saved — /bootstrap set")
        return
    print(dim(f"· running bootstrap [{source}]"))
    engine.session.log("bootstrap", source=source, chars=len(text))
    if sync:
        _send_turn(engine, fe, text, is_bootstrap=True)
    else:
        _run_turn(engine, fe, text, is_bootstrap=True)


def _agentic_report_cmd(engine: Engine, fe: TerminalFrontend) -> None:
    """/agentic_report: "Stats" runs the context folder's stats.sh as-is;
    "Index" pretty-prints KNOWLEDGE/INDEX.md and MEMORY/INDEX.md through
    the same markdown→ANSI renderer used for chat replies, instead of
    dumping raw markdown. Also the target of the status bar's "agentic
    report" click, gated on the same detection (see TuiFrontend._worker)."""
    root = memory.find_context_root(".")
    if root is None:
        print("· no context protocol folder detected here")
        return
    choice = select("Agentic report", [("stats", "Stats"), ("index", "Index")])
    if choice == "stats":
        print(memory.run_stats(root))
        return
    for rel in ("KNOWLEDGE/INDEX.md", "MEMORY/INDEX.md"):
        p = root / rel
        text = p.read_text(encoding="utf-8") if p.is_file() else "(missing)"
        print(f"\n{BOLD}{CYAN}{rel}{RESET}")
        renderer = mdrender.LineRenderer()
        for line in text.splitlines():
            print(renderer.render(line))


def _bootstrap_cmd(engine: Engine, fe: TerminalFrontend, arg: str) -> None:
    tokens = arg.split()
    sub = tokens[0].lower() if tokens else ""
    project = "project" in (t.lower() for t in tokens[1:])
    path_arg = next((t for t in tokens[1:] if t.lower() != "project"), "")

    if sub in ("", "run"):
        _run_bootstrap(engine, fe)
    elif sub == "show":
        text, source = bootstrap.load(".")
        url = bootstrap.source_url(".")
        origin = f" — from {url}" if url else ""
        print(f"· bootstrap [{source}]{origin}:\n\n{text}" if text
              else "· no bootstrap prompt saved — /bootstrap set")
    elif sub == "set":
        # a URL downloads and caches its content (the URL itself is
        # remembered so startup can offer "re-download" vs "run cached"); a
        # file path (as argument or pasted) saves that file's contents
        # (snapshot at set-time); otherwise paste lines, end with a lone
        # '.' (or Ctrl+D)
        url = None
        if path_arg and bootstrap.is_url(path_arg):
            print(dim(f"· downloading {path_arg} ..."))
            try:
                text = bootstrap.fetch_url(path_arg)
            except Exception as e:
                print(f"· download failed: {e}")
                return
            # R171/S3: the FIRST download of a URL has no integrity check at
            # all (no pinning/hash, follow_redirects=True) and this content
            # later gets offered as a tool-enabled turn at every startup —
            # show it before it's saved so a compromised URL or a redirect
            # attack doesn't get a free pass on the one tap that matters.
            print(dim(f"· fetched {len(text)} chars:"))
            print(text[:2000] + ("\n…[truncated]" if len(text) > 2000 else ""))
            if not confirm("Save and use this bootstrap prompt?"):
                print("· discarded — nothing saved")
                return
            src, url = path_arg, path_arg
        elif path_arg:
            text, src = bootstrap.from_input(path_arg)
            if src is None:
                print(f"· no such file: {path_arg}")
                return
        else:
            print(dim("paste the bootstrap prompt (or a file path/URL) — "
                      "end with a lone '.' line:"))
            lines = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                if line.strip() == ".":
                    break
                lines.append(line)
            pasted = "\n".join(lines)
            if bootstrap.is_url(pasted):
                print(dim(f"· downloading {pasted} ..."))
                try:
                    text = bootstrap.fetch_url(pasted)
                except Exception as e:
                    print(f"· download failed: {e}")
                    return
                print(dim(f"· fetched {len(text)} chars:"))
                print(text[:2000] +
                     ("\n…[truncated]" if len(text) > 2000 else ""))
                if not confirm("Save and use this bootstrap prompt?"):
                    print("· discarded — nothing saved")
                    return
                src, url = pasted, pasted
            else:
                text, src = bootstrap.from_input(pasted)
        if src:
            print(dim(f"· loaded contents of {src}"))
        if not text.strip():
            print("· empty — nothing saved")
            return
        p = bootstrap.save(text, project=project, cwd=".", source_url=url)
        print(f"· bootstrap saved → {p}")
    elif sub == "clear":
        p = bootstrap.clear(project=project, cwd=".")
        print(f"· removed {p}" if p else "· nothing to remove")
    else:
        print("· usage: /bootstrap [run|show|set [<file>|<url>] [project]|clear [project]]")


# ── /rewind (R47) ─────────────────────────────────────────────────────────
_COMMIT_DIFF_PREVIEW_CAP = 4000


def _commit_cmd(engine: Engine, fe: TerminalFrontend, arg: str) -> None:
    """/commit [message] (R101): stage relevant changes, draft (or use the
    given) commit message from the staged diff, show it, confirm, commit.
    Operates on the REAL project .git — never rewind.py's shadow repo."""
    cwd = "."
    if not gitcommit.is_repo(cwd):
        print("· not a git repository")
        return
    diff = gitcommit.staged_diff(cwd)
    if not diff.strip():
        pending = gitcommit.unstaged_summary(cwd)
        if not pending.strip():
            print("· nothing to commit — working tree clean")
            return
        print("· nothing staged. Unstaged/untracked changes:")
        print(dim(pending.rstrip()))
        if not confirm("Stage all changes and commit?", default_yes=False):
            print("· cancelled — stage what you want with `git add` first")
            return
        gitcommit.stage_all(cwd)
        diff = gitcommit.staged_diff(cwd)
        if not diff.strip():
            print("· nothing to commit")
            return
    shown = diff[:_COMMIT_DIFF_PREVIEW_CAP]
    if len(diff) > _COMMIT_DIFF_PREVIEW_CAP:
        shown += "\n… (truncated)"
    print(colour_diff(shown))

    message = arg.strip()
    if not message:
        print(dim("· drafting a commit message…"))
        try:
            message = gitcommit.draft_message(engine, diff,
                                              gitcommit.recent_log(cwd))
        except Exception as e:
            print(f"· draft failed: {e} — enter a message yourself")

    while True:
        print(f"\n{BOLD}commit message:{RESET}\n  " + (message or "(empty)"))
        choice = select("Commit with this message?", [
            ("y", "Yes, commit"),
            ("e", "Edit the message"),
            ("n", "Cancel — leave changes staged"),
        ])
        if choice == "y":
            if not message.strip():
                print("· refusing an empty commit message")
                continue
            print(f"· {gitcommit.commit(message, cwd)}")
            return
        if choice == "e":
            edited = input("new message: ").strip()
            if edited:
                message = edited
            continue
        print("· cancelled — changes remain staged")
        return


def _extension_tool_specs() -> list[dict]:
    """R119/R121: the tool specs that came from extensions specifically —
    `tools.specs()` also folds in the built-ins plus the `.agentic_context`
    doc tool when active, neither of which is an extension, so those are
    named out explicitly rather than assuming "everything past the built-ins"
    is extension-provided. Shared by `/extensions` and the startup banner so
    the two can never disagree about what's loaded.

    R157: `web_search`/`web_fetch` are deliberately NOT excluded any more.
    They now come from `extensions_bundled/web_extension.py`, so `/extensions`
    listing them is correct — it reports what is actually loaded, and a
    bundled extension is still an extension (the MCP tools have always shown
    up here the same way)."""
    from . import context as ctxmod
    non_ext_names = ({s["name"] for s in tools.SPEC}
                     | {s["name"] for s in ctxmod.SPEC})
    return [s for s in tools.specs() if s["name"] not in non_ext_names]


def _diff_cmd(engine: Engine) -> None:
    """/diff: what did the LAST turn actually change? Diffs the working tree
    against the checkpoint HEAD captured right before that turn ran
    (`engine.last_turn_diff_base`) — the shadow repo already has both
    endpoints (R47's per-mutation checkpoints), so this is the read-only
    other half of the approval gate: you approved N writes, here's what they
    did. Operates on rewind.py's shadow repo, never the project's real .git
    (that's /commit's job)."""
    if engine.last_turn_diff_base is None and not rewind.entries():
        print("· no checkpoints yet — one is taken before every approved "
              "write/edit/command; run a turn that mutates something first")
        return
    diff = rewind.diff_since(engine.last_turn_diff_base)
    if diff.startswith("[diff error:"):
        print(f"· {diff}")
        return
    if not diff.strip():
        print("· no changes since the last turn started")
        return
    print(colour_diff(diff))


def _rewind_cmd(arg: str) -> None:
    """List checkpoints (snapshots taken before every approved mutation) and
    restore one — `/rewind <id>` skips the picker. Restoring resets tracked
    files and deletes files created since; the pre-rewind state is itself
    checkpointed, so a rewind can be undone."""
    if arg:
        print(f"· {rewind.restore(arg)}")
        return
    rows = rewind.entries()
    if not rows:
        print("· no checkpoints yet — one is taken before every approved "
              "write/edit/command")
        return
    for i, r in enumerate(rows, 1):
        print(f"  {i}. {r['id']}  {r['age']:>8}  {r['label']}")
    raw = input("restore # (empty cancels): ").strip()
    if not raw:
        return
    if raw.isdigit() and 1 <= int(raw) <= len(rows):
        target = rows[int(raw) - 1]
        if confirm(f"Restore {target['id']}? Files changed since will be lost "
                   f"(a pre-rewind checkpoint is kept).", default_yes=False):
            print(f"· {rewind.restore(target['id'])}")
    else:
        print("· no such checkpoint")


def _undo_cmd() -> None:
    """/undo: revert just the LAST mutation. ALWAYS previews the affected
    paths before asking, and always NAMES them (never a generic "undo the
    last mutation?") — a real incident (twice, in one session) showed why:
    a mutation whose target was outside the checkpointed tree
    (`rewind.covers()`'s documented gap — an absolute path elsewhere on
    disk) left nothing in the whole-tree checkpoint to undo for it, and
    `undo()` used to fall back to reverting an OLDER, unrelated,
    already-sealed mutation instead — silently, with a confirm that named
    no files. R181's per-file snapshot (`rewind.snapshot_before_write`)
    closed the actual gap — write_file/edit_file/apply_patch are tracked
    regardless of location now — and `undo_preview()` dropped the
    unrelated-history fallback entirely (see its docstring for why it
    could never be correct). The filename is still always shown up front:
    naming it is what lets a user catch "that's not the file I meant"
    before confirming, not after. Feedback: naming the file wasn't quite
    enough either — showing the actual DIFF alongside it (via
    `rewind.undo_diff`) is what lets it be judged at a glance instead of
    just trusted."""
    kind, paths = rewind.undo_preview()
    if kind in ("none", "error"):
        print(f"· {rewind.undo()}")   # reuses undo()'s own wording for both
        return
    diff = rewind.undo_diff()
    if diff.strip():
        print(colour_diff(diff))
    if kind == "file":
        prompt = f"Undo will revert {paths[0]}. Proceed?"
    else:
        shown = ", ".join(paths[:8]) + (f" (+{len(paths) - 8} more)" if len(paths) > 8 else "")
        prompt = f"Undo will revert: {shown}. Proceed?"
    if confirm(prompt, default_yes=False):
        print(f"· {rewind.undo()}")


# a fixed banner + a rule between each section (R124a) — plain
# "[prompt]\n...\n\n[response]\n..." concatenation read as one run-on block
# with no visual seam between "the question" and "the answer"
_SEP = "-" * 10


def _raw_last_response_text(engine: Engine, fe: TerminalFrontend) -> str:
    """Last turn's RAW record (R124): the prompt that started it, the
    reasoning (if any), then the final answer. Unlike `/copy`/`engine.
    last_response()`, this deliberately includes the prompt and thinking —
    the one place either is allowed to leave the buffer/history."""
    prompt = engine.last_prompt()
    think = fe.think_buffer
    answer = engine.last_response()
    parts = []
    if prompt:
        parts.append(f"[prompt]\n{prompt}")
    if think:
        parts.append(f"[thinking]\n{think}")
    parts.append(f"[response]\n{answer}" if (prompt or think) else answer)
    if len(parts) == 1:
        return parts[0]
    return f"{_SEP}\n{_SEP}\n\n" + f"\n\n{_SEP}\n\n".join(parts)


def _last_copyable_text(engine: Engine, fe: TerminalFrontend) -> tuple[str, str]:
    """Whichever happened more recently: the last LLM turn's raw response
    (thinking included, via `_raw_last_response_text`) or — in the TUI's
    bash mode, which has no `engine`/session concept of its own — the last
    shell command's captured output, same text `!<cmd>` printed. Returns
    (text, label); label feeds the caller's copy-confirmation message.
    Falls back to LLM-only behavior for frontends without TUI bash state
    (e.g. the classic REPL, where `!<cmd>` isn't captured at all)."""
    tui = getattr(fe, "_tui", None)
    bash_text = getattr(tui, "_last_bash_output", "") if tui else ""
    bash_at = getattr(tui, "_last_bash_at", 0.0) if tui else 0.0
    llm_at = getattr(tui, "_last_llm_at", 0.0) if tui else 0.0
    llm_text = _raw_last_response_text(engine, fe)
    if bash_text and (not llm_text or bash_at >= llm_at):
        return bash_text, "command output"
    return llm_text, "raw response"


def _all_chat_text(engine: Engine) -> str:
    """The whole session transcript (questions + answers, tool calls) as
    markdown — same text `/export` writes to a file, via
    `session.export_markdown()`. No reasoning: matches the standing rule
    that thinking never enters history/`/copy`/exports."""
    return sessions.export_markdown(engine.session.id)


def _cost_report(engine: Engine) -> str:
    """`/cost` (R92, narrowed by R166): per-model token + $ breakdown
    ACROSS EVERY SESSION on this machine — a single session's breakdown
    lives in `/context <id>` now (its own tree, not a duplicate command),
    which frees `/cost` from the `[all]` argument that meant two different
    things depending on which command you were reading it in. Pure read
    over data Aurora already writes on every turn — no new accounting.

    Bug fix (2026-07-27): R168 made the status bar's `$` price CLICKABLE →
    runs this exact command — but the bar shows `engine.context_stats().
    cost_usd`, THIS SESSION's own accrued cost, while this report has always
    been the cross-session total (R166). Clicking "$3.31" and landing on a
    report whose bottom line says "$8.6359" reads as a miscalculation; both
    numbers were always correct for what they measure, there was just no
    line connecting them. The trailing note below states the current
    session's figure explicitly so the two numbers reconcile instead of
    just disagreeing."""
    from . import ctxtree
    rows = sessions.usage_all_sessions()
    if not rows:
        return "· no sessions yet"
    lines, total = ctxtree.model_breakdown_lines(rows)
    out = [f"{BOLD}token usage — all sessions{RESET}", *lines]
    # trim the trailing zeros BEFORE wrapping in colour codes — rstrip on the
    # wrapped string is a no-op with colours on and eats digits with them off
    total_str = f"${total:,.4f}".rstrip("0").rstrip(".")
    out.append(f"  {BOLD}total  {total_str}{RESET}")
    stats = engine.context_stats()
    if stats.cost_known:
        this_str = f"${stats.cost_usd:,.4f}".rstrip("0").rstrip(".")
        out.append(dim(f"  (this session so far: {this_str} — the number on "
                       f"the status bar; already included in the total above)"))
    return "\n".join(out)


def _handle_command(engine: Engine, fe: TerminalFrontend, line: str) -> bool:
    """Returns False to exit the REPL."""
    cmd, _, arg = line[1:].partition(" ")
    cmd, arg = cmd.strip().lower(), arg.strip()

    # `/<cmd> help` (alias `man`) — feature request, 2026-07-27: works for
    # EVERY command uniformly, checked once here rather than wired into
    # each branch below, so a new command gets it for free. Prints the
    # SAME text `--man`'s COMMANDS section shows for this command (one
    # source, `man.command_man` — see man.py's docstring), not the terse
    # one-line `COMMAND_INFO` blurb autocomplete already shows. Checked
    # before the exit/quit short-circuit so `/exit help` explains rather
    # than exiting. Trade-off, accepted: a command whose own argument
    # could legitimately BE the literal word "help"/"man" (e.g. searching
    # session logs for that word via `/search help`) can't reach that
    # argument this way — narrow enough not to block on.
    if arg.lower() in ("help", "man") and cmd in COMMAND_INFO:
        from . import man
        text = man.command_man(cmd)
        print(f"{CYAN}{BOLD}/{cmd}{RESET}\n{text}" if text
              else f"· {COMMAND_INFO[cmd]}")
        return True

    if cmd in ("exit", "quit"):
        return False
    if cmd == "help":
        print(help_text(memory.find_context_root(".") is not None))
    elif cmd == "model":
        sub, _, rest = arg.partition(" ")
        if sub.lower() == "add":
            _add_model_cmd(engine, rest.strip())
        elif sub.lower() in ("remove", "rm"):
            _remove_model_cmd(engine, rest.strip())
        else:
            _pick_model(engine, fe)
    elif cmd == "clear":
        engine.clear()
        print("· history cleared")
    elif cmd == "reset":
        engine.reset()
        print("· full reset — history and system prompt cleared")
        if bootstrap.load(".")[0] and confirm("Re-run bootstrap?"):
            _run_bootstrap(engine, fe)
    elif cmd == "compact":
        n = engine.compact_history(notify=fe.notify)
        print(f"· compacted {n} messages into one" if n else "· nothing to compact")
    elif cmd == "copy":
        n = int(arg) if arg.isdigit() else 1
        text = engine.nth_response(n)
        if not text:
            print("· no such response")
        else:
            print(f"· copied via {clipboard.copy(text)}")
    elif cmd == "copy-last":
        text, label = _last_copyable_text(engine, fe)
        if not text:
            print("· no such response")
        else:
            suffix = " (thinking included)" if label == "raw response" else ""
            print(f"· {label}{suffix} copied via {clipboard.copy(text)}")
    elif cmd == "copy-all":
        text = _all_chat_text(engine)
        if not text.strip():
            print("· nothing to copy yet")
        else:
            print(f"· whole chat copied via {clipboard.copy(text)}")
    elif cmd == "redact":
        if arg.lower() == "allowlist":
            n = len(engine.secret_allowlist)
            print(f"· {n} allowlisted value{'s' if n != 1 else ''} "
                 f"(usage: /redact allowlist clear)")
        elif arg.lower() == "allowlist clear":
            engine.clear_secret_allowlist()
            print("· allowlist cleared")
        else:
            if arg.lower() in ("on", "off"):
                engine.set_redact_secrets(arg.lower() == "on")
            print(f"· secret redaction {'ON' if engine.redact_secrets else 'OFF'} "
                 f"(persisted; usage: /redact on|off | /redact allowlist [clear])")
    elif cmd == "cost":
        if arg.strip():
            # R171/I1: /cost (all sessions) and /context <id> (one session's
            # tree) are two commands both named around "cost" with different
            # scopes — `/cost <id>` doing what `/context <id>` does removes
            # the need to remember which command takes an id.
            from . import ctxtree
            print(ctxtree.report(engine, arg))
        else:
            print(_cost_report(engine))
    elif cmd == "context":
        from . import ctxtree
        print(ctxtree.report(engine, arg))
    elif cmd == "cache":
        if arg.lower() in ("on", "off"):
            engine.set_prompt_cache(arg.lower() == "on")
        state = "ON" if engine.prompt_cache else "OFF"
        active = "yes" if engine.cache_enabled() else "no (this model)"
        print(f"· prompt caching {state} (persisted) · active on the current "
              f"model: {active}\n"
              + dim("  marks the system prompt as cacheable so the bootstrap "
                    "preamble isn't re-billed every tool iteration; /cost "
                    "shows the hits"))
    elif cmd == "autocompact":
        if arg.lower() in ("on", "off"):
            engine.set_auto_compact(arg.lower() == "on")
        state = "ON" if engine.auto_compact else "OFF"
        print(f"· auto-compact {state} (persisted) — folds older history "
              f"once context hits {engine.auto_compact_threshold_pct:.0f}%, "
              f"keeping the last ~{engine.compact_keep_recent_tokens} tokens "
              "raw")
    elif cmd == "fallback":
        if arg.lower() in ("on", "off"):
            engine.set_model_fallback(arg.lower() == "on")
        state = "ON" if engine.model_fallback else "OFF"
        chain = ", ".join(m.get("model", "?") for m in engine._fallback_models())
        print(f"· model fallback {state} (persisted) — on a hard provider "
              f"failure, retries the turn against the next model with a "
              f"usable key\n" + dim(f"  chain from {engine.current.get('model')}: "
                                   f"{chain or '(no other model with a usable key)'}"))
    elif cmd == "multiline":
        engine.set_multiline(not engine.multiline)
        print(f"· multiline {'ON' if engine.multiline else 'OFF'} "
              f"(Enter inserts newline, Alt+Enter submits; persisted)")
    elif cmd == "thinking":
        fe.show_thinking = not fe.show_thinking
        print(f"· live thinking view {'ON (dim stream)' if fe.show_thinking else 'OFF (marker only)'}")
    elif cmd == "markdown":
        fe.render_md = not fe.render_md
        print(f"· markdown rendering {'ON' if fe.render_md else 'OFF (raw text)'}")
    elif cmd == "status":
        h = engine.provider_health()
        mark = f"{GREEN}✔{RESET}" if h["ok"] else f"{RED}✘{RESET}"
        print(f"  {mark} {engine.current.get('model')} — {h['detail']}")
    elif cmd == "allowlist":
        data = approve.load()
        legacy = set(approve.legacy_rules(data))
        for k, v in data.items():
            print(f"  {k}:")
            for rule in v:
                mark = dim("  (legacy single-token — exact match only; "
                           "consider removing)") if rule in legacy else ""
                print(f"    - {rule}{mark}")
            if not v:
                print("    (empty)")
        print(f"· edit: {approve._path()}")
    elif cmd == "denylist":
        data = approve.load_deny()
        if not data:
            print("  (empty — nothing is denied by policy)")
        for k, v in data.items():
            print(f"  {k}:")
            for rule in v:
                print(f"    - {rule}")
        print(f"· edit: {approve._deny_path()}")
    elif cmd == "rewind":
        _rewind_cmd(arg)
    elif cmd == "undo":
        _undo_cmd()
    elif cmd == "diff":
        _diff_cmd(engine)
    elif cmd == "commit":
        _commit_cmd(engine, fe, arg)
    elif cmd == "resume":
        rows = sessions.list_sessions()
        if not rows:
            print("· no past sessions")
            return True
        for i, (sid, mtime, first) in enumerate(rows, 1):
            print(f"  {i}. {sid}  {mtime}  {first}")
        raw = input("session #: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(rows):
            sid = rows[int(raw) - 1][0]
            n = engine.resume_from(sid)
            print(f"· resumed {sid} ({n} turns)")
        elif raw:
            print("· no such session — enter a number from the list, or empty to cancel")
    elif cmd == "search":
        if not arg:
            print("· usage: /search <text>")
            return True
        rows = sessions.search_sessions(arg)
        if not rows:
            print("· no matches")
            return True
        for i, (sid, mtime, event, snippet) in enumerate(rows, 1):
            print(f"  {i}. {sid}  {mtime}  ({event}) …{snippet}…")
        raw = input("resume session #, or empty to cancel: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(rows):
            sid = rows[int(raw) - 1][0]
            n = engine.resume_from(sid)
            print(f"· resumed {sid} ({n} turns)")
        elif raw:
            print("· no such session — enter a number from the list, or empty to cancel")
    elif cmd == "export":
        out = f"aurora-session-{engine.session.id}.md"
        with open(out, "w") as f:
            f.write(sessions.export_markdown(engine.session.id))
        print(f"· wrote {out}")
    elif cmd == "skills":
        print(skills.listing(engine.cfg.get("_base_dir")))
    elif cmd == "extensions" and arg.split(" ", 1)[0] == "new":
        ext_name = arg.split(" ", 1)[1].strip() if " " in arg else ""
        if not ext_name:
            print("· usage: /extensions new <name>")
        else:
            try:
                path = extensions.scaffold(ext_name)
                print(f"· wrote {path}\n"
                     f"  edit SPEC + the tool function, then restart Aurora to load it")
            except FileExistsError as e:
                print(f"· already exists: {e}")
            except ValueError as e:
                print(f"· {e}")
    elif cmd == "extensions":
        ext_specs = _extension_tool_specs()
        if not ext_specs:
            print("· no extensions loaded")
        else:
            print(f"· {len(ext_specs)} extension tool"
                 f"{'s' if len(ext_specs) != 1 else ''} loaded:")
        for s in ext_specs:
            print(f"  {s['name']}  {dim(s['description'])}")
        if engine.extension_warnings:
            print("· warnings:")
            for w in engine.extension_warnings:
                print(f"    - {w}")
        print(dim(
            "  bundled with Aurora: mcp_* (config.yaml's mcp_servers:), "
            "lint_check\n"
            "  add your own: drop a .py file (SPEC + RUNNERS) into "
            f"{aurora_home() / 'extensions'}\n"
            "  full docs: documents/EXTENSIONS.md in the Aurora repo"))
    elif cmd == "bootstrap":
        _bootstrap_cmd(engine, fe, arg)
    elif cmd == "remember":
        memory.remember(engine, fe, arg)
    elif cmd == "agentic_report":
        _agentic_report_cmd(engine, fe)
    elif cmd == "nano":
        tui = getattr(fe, "_tui", None)
        if tui is None:
            print("· /nano only works in the full-screen TUI")
        elif not arg:
            print("· usage: /nano <file>")
        else:
            from pathlib import Path
            # check_busy=False: this call IS the worker's own dispatch of
            # `/nano` (the worker set self._busy True for this very line
            # right before reaching here), so the busy guard — meant for
            # the OTHER entry point, a mouse click racing an unrelated
            # command — would always find "itself" busy and self-block.
            tui.open_nano(Path(arg).expanduser(), check_busy=False)
    else:  # /name args → a skill (R11)
        print(skills.run(cmd, arg, engine.cfg.get("_base_dir")))
    return True


def _banner(engine: Engine) -> None:
    """Clear the screen and show a compact session card at startup."""
    import os

    from . import __version__ as version

    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")  # clear + cursor home

    h = engine.provider_health()
    mark = f"{GREEN}✔{RESET}" if h["ok"] else f"{RED}✘{RESET}"

    info_lines = [f"{CYAN}{BOLD}Aurora{RESET} {dim(version)}",
                  f"  model    {BOLD}{engine.current.get('model')}{RESET}  "
                  f"{mark} {dim(h['detail'])}",
                  f"  cwd      {os.getcwd()}",
                  f"  session  {engine.session.id}"
                  + (dim(f"  ({len(engine.messages)} messages resumed)")
                     if engine.messages else "")]
    n_ext = len(_extension_tool_specs())
    if n_ext:
        info_lines.append(f"  extensions  {n_ext} tool"
                          f"{'s' if n_ext != 1 else ''} loaded"
                          + dim("  (/extensions for details)"))
    print("\n".join(info_lines))
    print()


# ── main loop ─────────────────────────────────────────────────────────────
def run(engine: Engine) -> None:
    fe = TerminalFrontend(
        show_thinking=bool(engine.runtime.get("show_thinking", False)),
        render_md=bool(engine.runtime.get("render_markdown", True)))
    keystore.set_prompter(fe.ask_secret)  # engine key prompts go through us

    # the agent starts with NO project knowledge — only the user's saved
    # bootstrap prompt (below) introduces any init ritual
    _banner(engine)

    # startup ask: fires whenever a non-empty bootstrap prompt exists;
    # skipped when resuming (--continue) — the session already has context
    bp_text, bp_source = bootstrap.load(".")
    if bp_text and not engine.messages:
        first = next((l for l in bp_text.splitlines() if l.strip()), "")
        bp_url = bootstrap.source_url(".")
        print(f"{YELLOW}bootstrap prompt{RESET} [{bp_source}] "
              + dim(f"{len(bp_text)} chars"))
        print(dim(f"  “{_short(first, 70)}”"))
        try:
            choice = _bootstrap_run_choice(bp_url)
        except (EOFError, KeyboardInterrupt):
            choice = "skip"
        if choice == "run":
            _run_bootstrap(engine, fe)
        elif choice == "download":
            _run_bootstrap(engine, fe, redownload=True)

    kb = KeyBindings()
    _ml_on = Condition(lambda: engine.multiline)
    _ml_off = Condition(lambda: not engine.multiline)

    @kb.add("c-j")       # Ctrl+J newline
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("space")
    def _(event):
        buf = event.current_buffer
        if _expand_typed_newline(buf):
            return
        buf.insert_text(" ")

    @kb.add("enter", filter=_ml_off)
    def _(event):
        event.current_buffer.validate_and_handle()

    @kb.add("enter", filter=_ml_on)
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "enter", filter=_ml_on)
    def _(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "m")
    def _(event):
        engine.set_multiline(not engine.multiline)
        fe.notify(f"multiline {'ON (Enter newline, Alt+Enter submit)' if engine.multiline else 'OFF'}")

    ps = PromptSession(key_bindings=kb, bottom_toolbar=_footer(engine),
                       completer=SlashCompleter(engine.cfg.get("_base_dir")),
                       complete_while_typing=True, enable_suspend=True)

    while True:
        try:
            with patch_stdout():
                line = ps.prompt(_prompt_message())
        except KeyboardInterrupt:
            continue  # Ctrl+C at the prompt: just redraw (R17)
        except EOFError:
            break
        line = _expand_newlines(line).strip()
        if not line:
            continue
        if line == "?":  # same gesture as ! for bash mode
            print(help_text(memory.find_context_root(".") is not None))
            continue
        if line.startswith("!"):  # local bash, no LLM (R10)
            subprocess.run(line[1:], shell=True)
            continue
        if line.startswith("/"):
            if not _handle_command(engine, fe, line):
                break
            continue
        _run_turn(engine, fe, line)

    print(f"\nResume this session with:\n  aurora --resume {engine.session.id}")
    print("bye")
