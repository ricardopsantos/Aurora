"""Terminal front end — a prompt_toolkit REPL implementing frontend.Frontend.

Owns ALL terminal I/O: streaming, footer (R13), keybindings (Shift+Enter /
Cmd+Enter newline, ? help, R18), Ctrl+C interrupt (R17), slash commands,
`!cmd` passthrough (R10), the /model picker incl. the LlamaDesk library with
its eviction confirm (R3). The engine is driven only through its public
methods.
"""

import getpass
import subprocess
import sys
import threading

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from . import (approve, bootstrap, clipboard, keystore, mdrender, memory,
               rewind, session as sessions, skills)
from .colors import (BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW,
                     colour_diff, dim)
from .engine import Engine
from .llamadesk import LlamaDesk, LlamaDeskError

HELP = f"""\
{BOLD}plain text{RESET}            talk to the model (paste is safe)
{BOLD}!{RESET} / {BOLD}!cmd{RESET}              bash: `!` on an empty line enters bash mode ($);
                      each Enter runs a shell command locally, no LLM
                      (Esc or empty backspace exits). Classic REPL: `!cmd`
{CYAN}/model{RESET}                model picker (Anthropic $ · OpenRouter $ · local library)
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
{CYAN}/think  /thinking{RESET}     show last turn's reasoning · toggle live dim stream
{CYAN}/markdown{RESET}             toggle pretty rendering (bold/code/bullets) vs raw text
{CYAN}/allowlist{RESET}            show the persistent approval allowlist
{CYAN}/rewind [id]{RESET}          restore the tree to a pre-mutation checkpoint
{CYAN}/resume  /export{RESET}      pick a past session · dump conversation as markdown
{CYAN}/skills  /name args{RESET}   list skills · run one
{CYAN}/bootstrap{RESET}            run the saved bootstrap prompt (set/show/clear to manage)
{CYAN}/help  /quit{RESET}          this help · quit immediately (Esc Esc asks first)"""


class TerminalFrontend:
    """The Frontend implementation (see frontend.py for the contract)."""

    def __init__(self, show_thinking: bool = False, render_md: bool = True):
        self.cancel_event = threading.Event()
        self.show_thinking = show_thinking   # live dim stream vs marker only
        self.render_md = render_md           # pretty markdown (display only)
        self.think_buffer = ""               # last turn's reasoning, for /think
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
            sys.stdout.write(dim("(thinking… — /think to read it after)"))
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
        else:
            print(f"  {BOLD}{args.get('path', '')}{RESET}")
        if diff:
            print(colour_diff(diff))
        key = select("Approve?", [
            ("y", "Yes, run once"),
            ("a", "Always allow this (remember)"),
            ("n", "No, skip"),
            ("s", "Stop the agent"),
            ("c", "Comment — steer the model instead"),
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
        return self._ask_keep_going(
            f"{iterations} tool iterations — continue?", allow_silent=True)

    def ask_secret(self, label: str) -> str:
        return getpass.getpass(label).strip()

    def secret_challenge(self, context: str, matches: list,
                         source_text: str = "") -> str:
        from . import secrets as secretscan
        where = "your prompt" if context == "prompt" else f"`{context[5:]}` output"
        print(f"\n{RED}{BOLD}── possible secret detected — {where} ────{RESET}")
        print(f"  {secretscan.preview(matches)}")
        if source_text:
            for line in secretscan.format_matches(source_text, matches):
                print(f"  {line}")
        return select("What should Aurora do?", [
            ("redact", "Replace with <secret> and continue"),
            ("keep", "Keep as-is and continue"),
            ("always", "Always allow this value — never flag it again"),
            ("stop", "Stop"),
        ])

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
        cost = f" │ ${s.cost_usd:.2f}" if s.cost_usd else ""
        warn = "  ⚠ context >80% — /compact?" if s.pct >= 80 else ""
        ml = " │ multiline" if engine.multiline else ""
        return (f" {s.model} │ ctx {used}/{limit} ({s.pct:.0f}%)"
                f"{cost} │ session {s.session_id}{warn}{ml}\n"
                + _FOOTER_HINT)
    return render


def _prompt_message():
    """A dim full-width rule above the input — separates the prompt area
    from the chat scroll, à la Claude Code / Copilot."""
    import shutil
    width = shutil.get_terminal_size((80, 24)).columns
    return [("fg:ansibrightblack", "─" * width + "\n"),
            ("bold fg:ansicyan", "> ")]


def _run_turn(engine: Engine, fe: TerminalFrontend, text: str,
              is_bootstrap: bool = False) -> None:
    """Run engine.send in a worker so the main thread can catch Ctrl+C (R17)."""
    fe.cancel_event.clear()
    fe.begin_turn()
    err: list[BaseException] = []

    def work():
        try:
            engine.send(text, fe, bootstrap=is_bootstrap)
        except BaseException as e:  # surface, don't kill the REPL
            err.append(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    while t.is_alive():
        try:
            t.join(0.1)
        except KeyboardInterrupt:
            fe.cancel_event.set()
    fe.end_turn()
    if err:
        print(f"\n{RED}✗ {err[0].__class__.__name__}: {err[0]}{RESET}")
    print()


# ── /model picker ─────────────────────────────────────────────────────────
# /model must feel as instant as /exit — it's not an LLM call, just a local
# menu. A recently-unreachable LlamaDesk shouldn't make every /model pay the
# probe's timeout again: remember the failure for a short while and skip
# straight past it (still retried automatically once the TTL expires, in
# case the box came back).
_LLAMADESK_RECHECK_S = 30
_llamadesk_last_fail: dict[str, float] = {}


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


def _llamadesk(engine: Engine) -> LlamaDesk | None:
    cfg = engine.cfg.get("llamadesk") or {}
    url = cfg.get("url")
    if not url:
        return None
    # cache key MUST match LlamaDesk.__init__'s own normalization (it
    # rstrips "/"), or a mark_failed(desk.base_url) after construction would
    # never hit the same key this lookup checks
    key = url.rstrip("/")
    import time
    last_fail = _llamadesk_last_fail.get(key)
    if last_fail is not None and time.monotonic() - last_fail < _LLAMADESK_RECHECK_S:
        return None   # skip the probe entirely — no network call, no wait
    token = ""
    env = cfg.get("token_env")
    if env:
        token = keystore.get_key(env, interactive=False) or ""
    return LlamaDesk(url, token)


def _llamadesk_mark_failed(url: str) -> None:
    import time
    _llamadesk_last_fail[url] = time.monotonic()


def _llamadesk_mark_ok(url: str) -> None:
    _llamadesk_last_fail.pop(url, None)


def _pick_model(engine: Engine, fe: TerminalFrontend) -> None:
    desk = _llamadesk(engine)
    loaded = None
    if desk:
        try:
            loaded = desk.loaded_model()
            _llamadesk_mark_ok(desk.base_url)
        except LlamaDeskError as e:
            _llamadesk_mark_failed(desk.base_url)
            desk = None
            print(f"{YELLOW}· LlamaDesk unreachable: {e}{RESET}")

    from .providers.anthropic import MODELS as ANTHROPIC_MODELS

    def _info(ctx: int | None, size: int | None = None) -> str:
        parts = []
        if size:
            parts.append(f"{size / 1e9:.1f}GB")
        if ctx:
            parts.append(f"{ctx // 1000}k ctx")
        return dim(" · ".join(parts)) if parts else ""

    details: list[dict] = []
    natives: dict[str, int | None] = {}
    sizes: dict[str, int | None] = {}
    if desk:
        try:
            details = desk.models_detail()
            for md in details:
                natives[md.get("name", "")] = md.get("ctx_native")
                sizes[md.get("name", "")] = md.get("size_bytes")
        except LlamaDeskError as e:
            desk = None
            print(f"{YELLOW}· LlamaDesk unreachable: {e}{RESET}")

    entries = []       # (label, kind, payload)
    current_index = 0  # which entry is the active model — pre-highlighted
    # identity (`is`) isn't reliable here: switch_model() stores whatever dict
    # it was handed, which is rarely the SAME object as the matching entry in
    # engine.list_models() (a fresh copy parsed from config.yaml) — compare by
    # the (provider, model) pair instead.
    cur_key = (engine.current.get("provider"), engine.current.get("model"))
    # alphabetical picker: config models A→Z, then the LlamaDesk library A→Z
    for m in sorted(engine.list_models(),
                    key=lambda m: str(m.get("model", "")).lower()):
        name = m.get("model", "")
        paid = name != "local" and (engine.provider_kind(m) == "anthropic" or
            "openrouter" in str(m.get("provider", "")))
        tag = f"{YELLOW}[$]{RESET}" if paid else f"{GREEN}[free]{RESET}"
        info = _info(ANTHROPIC_MODELS.get(name, (None,))[0]) if paid else ""
        if name == "local":  # show what "local" actually is
            live = loaded
            if not live:
                provider = engine._provider_for(m)
                live = provider.live_model_name() \
                    if hasattr(provider, "live_model_name") else None
            if live:
                name = f"local {DIM}→ {live}{RESET}"
                info = _info(natives.get(live), sizes.get(live))
        is_current = (m.get("provider"), m.get("model")) == cur_key
        mark = f"  {GREEN}{BOLD}✔{RESET}" if is_current else ""
        no_key = f"  {RED}(no key set){RESET}" if not engine.has_key(m.get("provider")) else ""
        if is_current:
            current_index = len(entries)
        entries.append((f"{name}{mark}  {tag} {info}{no_key}", "config", m))

    for md in sorted(details, key=lambda d: str(d.get("name", "")).lower()):
        name = md.get("name", "")
        note = (f"{GREEN}loaded, ready{RESET}" if name == loaded
                else dim("library — needs a ~1-2 min load"))
        entries.append((f"local:{name}  {GREEN}[free]{RESET} "
                        f"{_info(natives.get(name), sizes.get(name))} {note}",
                        "library", name))

    options = [(str(i), label) for i, (label, _, _) in enumerate(entries, 1)]
    chosen = select("Select model", options, default_index=current_index)
    idx = int(chosen) - 1
    _, kind, payload = entries[idx]

    if kind == "config":
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
        return

    # LlamaDesk library model (R3): eviction confirm, then load + wait
    name = payload
    if name != loaded:
        print(f"{YELLOW}⚠ switching the local model is GLOBAL — it evicts the "
              f"current model for every other consumer of that server.{RESET}")
        try:
            if desk.busy():
                print("✗ LlamaDesk reports a switch in progress — not switching.")
                return
        except LlamaDeskError:
            pass
        if not confirm(f"Load '{name}' and evict '{loaded}'?", default_yes=False):
            return
        try:
            # load at min(model's native ctx, configured target) — never rope-
            # extend past what the model was trained for
            ctx = int((engine.cfg.get("llamadesk") or {}).get("ctx", 65536))
            native = natives.get(name)
            if native:
                ctx = min(ctx, native)
            print(dim(f"  loading with ctx {ctx}"))
            desk.switch(name, ctx=ctx)
            print("loading", end="", flush=True)
            ok = desk.wait_ready(name, on_tick=lambda: print(".", end="", flush=True))
            print()
            if not ok:
                print("✗ model did not come up in time")
                return
        except LlamaDeskError as e:
            print(f"✗ {e}")
            return
    # point the local provider entry at it
    local = next((m for m in engine.list_models()
                  if engine.provider_kind(m) != "anthropic"
                  and "openrouter" not in str(m.get("provider", ""))), None)
    if local:
        local = dict(local, model=name)
        engine.switch_model(local)
        print(f"{GREEN}→ local:{name}{RESET}")


COMMAND_INFO = {
    "model":     "model picker — Anthropic $ · OpenRouter $ · local library",
    "status":    "is the current model's backend up and ready?",
    "think":     "show last turn's reasoning",
    "thinking":  "toggle the live dim reasoning stream",
    "markdown":  "toggle pretty rendering vs raw text",
    "compact":   "summarize-and-continue (frees context)",
    "clear":     "start fresh — history cleared",
    "reset":     "clear history + system prompt; offers /bootstrap",
    "copy":      "copy Nth-last response to the clipboard",
    "copy-last": "copy last turn's RAW response (thinking included) to the clipboard",
    "copy-all":  "copy the whole chat (questions + answers) to the clipboard",

    "allowlist": "show the persistent approval allowlist",
    "rewind":    "restore the working tree to a pre-mutation checkpoint",
    "resume":    "pick a past session to continue",
    "export":    "dump this conversation as markdown",
    "skills":    "list installed skills",
    "bootstrap": "run the saved bootstrap prompt (set/show/clear)",
    "multiline": "toggle multiline mode: Enter newline, Alt+Enter submit (persisted)",
    # "remember" deliberately hidden from autocomplete/discovery while it's
    # being reworked — the command itself still works if typed (see
    # `elif cmd == "remember":` below); see MEMORY for the follow-up note.
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
        for name, path in sorted(skills.discover(self.config_base).items()):
            entries.setdefault(name, skills._blurb(path) or "installed skill")
        for name, info in sorted(entries.items(), key=lambda kv: kv[0].lower()):
            if name.startswith(prefix):
                if empty:
                    yield Completion(f"/{name}", start_position=0,
                                     display=f"/{name}", display_meta=info)
                else:
                    yield Completion(name, start_position=-len(prefix),
                                     display_meta=info)


# ── /bootstrap ────────────────────────────────────────────────────────────
def _run_bootstrap(engine: Engine, fe: TerminalFrontend) -> None:
    text, source = bootstrap.load(".")
    if not text:
        print("· no bootstrap prompt saved — /bootstrap set")
        return
    print(dim(f"· running bootstrap [{source}]"))
    engine.session.log("bootstrap", source=source, chars=len(text))
    _run_turn(engine, fe, text, is_bootstrap=True)


def _bootstrap_cmd(engine: Engine, fe: TerminalFrontend, arg: str) -> None:
    tokens = arg.split()
    sub = tokens[0].lower() if tokens else ""
    project = "project" in (t.lower() for t in tokens[1:])
    path_arg = next((t for t in tokens[1:] if t.lower() != "project"), "")

    if sub in ("", "run"):
        _run_bootstrap(engine, fe)
    elif sub == "show":
        text, source = bootstrap.load(".")
        print(f"· bootstrap [{source}]:\n\n{text}" if text
              else "· no bootstrap prompt saved — /bootstrap set")
    elif sub == "set":
        # a file path (as argument or pasted) saves that file's contents
        # (snapshot at set-time); otherwise paste lines, end with a lone
        # '.' (or Ctrl+D)
        if path_arg:
            text, src = bootstrap.from_input(path_arg)
            if src is None:
                print(f"· no such file: {path_arg}")
                return
        else:
            print(dim("paste the bootstrap prompt (or a file path) — "
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
            text, src = bootstrap.from_input("\n".join(lines))
        if src:
            print(dim(f"· loaded contents of {src}"))
        if not text.strip():
            print("· empty — nothing saved")
            return
        p = bootstrap.save(text, project=project, cwd=".")
        print(f"· bootstrap saved → {p}")
    elif sub == "clear":
        p = bootstrap.clear(project=project, cwd=".")
        print(f"· removed {p}" if p else "· nothing to remove")
    else:
        print("· usage: /bootstrap [run|show|set [<file>] [project]|clear [project]]")


# ── /rewind (R47) ─────────────────────────────────────────────────────────
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


# ── slash commands ────────────────────────────────────────────────────────
def _raw_last_response_text(engine: Engine, fe: TerminalFrontend) -> str:
    """Last turn's RAW response: reasoning (if any) followed by the final
    answer. Unlike `/copy`/`engine.last_response()`, this deliberately
    includes thinking — the one place it's allowed to leave the buffer."""
    think = fe.think_buffer
    answer = engine.last_response()
    return f"[thinking]\n{think}\n\n[response]\n{answer}" if think else answer


def _all_chat_text(engine: Engine) -> str:
    """The whole session transcript (questions + answers, tool calls) as
    markdown — same text `/export` writes to a file, via
    `session.export_markdown()`. No reasoning: matches the standing rule
    that thinking never enters history/`/copy`/exports."""
    return sessions.export_markdown(engine.session.id)


def _handle_command(engine: Engine, fe: TerminalFrontend, line: str) -> bool:
    """Returns False to exit the REPL."""
    cmd, _, arg = line[1:].partition(" ")
    cmd, arg = cmd.strip().lower(), arg.strip()

    if cmd in ("exit", "quit"):
        return False
    if cmd == "help":
        print(HELP)
    elif cmd == "model":
        _pick_model(engine, fe)
    elif cmd == "clear":
        engine.clear()
        print("· history cleared")
    elif cmd == "reset":
        engine.reset()
        print("· full reset — history and system prompt cleared")
        if bootstrap.load(".")[0]:
            if confirm("Re-run bootstrap?"):
                _run_bootstrap(engine, fe)
    elif cmd == "compact":
        n = engine.compact_history()
        print(f"· compacted {n} messages into one" if n else "· nothing to compact")
    elif cmd == "copy":
        n = int(arg) if arg.isdigit() else 1
        text = engine.nth_response(n)
        if not text:
            print("· no such response")
        else:
            print(f"· copied via {clipboard.copy(text)}")
    elif cmd == "copy-last":
        text = _raw_last_response_text(engine, fe)
        if not text:
            print("· no such response")
        else:
            print(f"· raw response (thinking included) copied via {clipboard.copy(text)}")
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
    elif cmd == "multiline":
        engine.set_multiline(not engine.multiline)
        print(f"· multiline {'ON' if engine.multiline else 'OFF'} "
              f"(Enter inserts newline, Alt+Enter submits; persisted)")
    elif cmd == "think":
        if fe.think_buffer:
            print(dim(fe.think_buffer))
        else:
            print("· no thinking captured on the last turn")
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
    elif cmd == "rewind":
        _rewind_cmd(arg)
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
    elif cmd == "export":
        out = f"aurora-session-{engine.session.id}.md"
        with open(out, "w") as f:
            f.write(sessions.export_markdown(engine.session.id))
        print(f"· wrote {out}")
    elif cmd == "skills":
        print(skills.listing(engine.cfg.get("_base_dir")))
    elif cmd == "bootstrap":
        _bootstrap_cmd(engine, fe, arg)
    elif cmd == "remember":
        memory.remember(engine, fe)
    else:  # /name args → a skill (R11)
        print(skills.run(cmd, arg, engine.cfg.get("_base_dir")))
    return True


def _banner(engine: Engine) -> None:
    """Clear the screen and show a compact session card at startup."""
    import os
    from . import __version__ as version
    from . import logo

    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")  # clear + cursor home

    h = engine.provider_health()
    mark = f"{GREEN}✔{RESET}" if h["ok"] else f"{RED}✘{RESET}"

    info_lines = [f"{CYAN}{BOLD}Aurora{RESET} {dim('v' + version)}",
                  f"  model    {BOLD}{engine.current.get('model')}{RESET}  "
                  f"{mark} {dim(h['detail'])}",
                  f"  cwd      {os.getcwd()}",
                  f"  session  {engine.session.id}"
                  + (dim(f"  ({len(engine.messages)} messages resumed)")
                     if engine.messages else ""),
                  dim("  /help · /model · ? help · --man manual")]

    logo_path = logo.resolve_logo(engine.cfg)
    logo_lines: list[str] = []
    logo_w = 0
    if logo_path:
        try:
            logo_lines = logo.render(logo_path, max_rows=10, max_cols=22)
            logo_w = logo.visible_width(logo_lines[0]) if logo_lines else 0
        except Exception as e:
            info_lines.append(dim(f"  logo: {e}"))

    merged: list[str] = []
    for i in range(max(len(logo_lines), len(info_lines))):
        left = logo_lines[i] if i < len(logo_lines) else " " * logo_w
        right = info_lines[i] if i < len(info_lines) else ""
        merged.append(f"{left}  {right}")
    print("\n".join(merged))
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
        print(f"{YELLOW}bootstrap prompt{RESET} [{bp_source}] "
              + dim(f"{len(bp_text)} chars"))
        print(dim(f"  “{_short(first, 70)}”"))
        try:
            go = confirm("Run the bootstrap prompt?")
        except (EOFError, KeyboardInterrupt):
            go = False
        if go:
            _run_bootstrap(engine, fe)

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
            print(HELP)
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
