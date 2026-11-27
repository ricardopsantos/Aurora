"""Entry point: `aurora` | `python -m aurora`.

  aurora                     start
  aurora --continue          resume the most recent session
  aurora --resume ID         resume a specific session
  aurora --classic           inline REPL instead of the full-screen TUI
  aurora -t "task"           one-shot, non-interactive (-t - reads stdin);
                             [--yes] [--json] [--max-steps N]; exit 0 ok / 1 incomplete / 2 usage
  aurora --web               Aurora Lite: a small web chat (R353); [--bind ADDR] [--port N]
                             [--workspace DIR]; needs AURORA_WEB_TOKEN
  aurora --debug             tint TUI areas red so their bounds are visible
  aurora <config.yaml>       alternate config
  aurora key set [ENV_VAR]   store an API key (keyring / encrypted file)
  aurora key status [ENV_VAR] show whether a key is set and where from
                             (no ENV_VAR = every key this config.yaml uses)
  aurora key clear [ENV_VAR] remove a stored key (or --all for every one
                             configured with api_key_env/token_env)
  aurora wipe                delete AURORA_HOME — logs out of every
                             provider and resets sessions/allowlist/state
"""

import os
import sys
from pathlib import Path
from typing import NoReturn

import yaml

from . import session as sessions


def _default_config() -> str:
    """AURORA_CONFIG when set (R315), else config.yaml next to the package (repo checkout), else AURORA_HOME."""
    env = os.environ.get("AURORA_CONFIG")
    if env:
        if not Path(env).is_file():
            sys.exit(f"AURORA_CONFIG is not a file: {env}")
        return env
    repo = Path(__file__).resolve().parent.parent / "config.yaml"
    if repo.is_file():
        return str(repo)
    from .paths import aurora_home
    home_cfg = aurora_home() / "config.yaml"
    if home_cfg.is_file():
        return str(home_cfg)
    sys.exit("no config.yaml found (repo root or AURORA_HOME) — create it with "
             f"`cp {repo.with_name('config.yaml.example')} {repo}` (or run ./install.sh), "
             "then adjust providers and models")


def _load_raw_config() -> dict:
    try:
        from .config import read_raw_config
        return read_raw_config(_default_config())
    except Exception:
        return {}


def _fetch_command(env: str) -> str | None:
    """config.yaml key_fetch: <ENV_VAR> → shell command that prints the value
    (e.g. an ssh to wherever it lives). The command is shown and only runs on
    explicit approval."""
    return (_load_raw_config().get("key_fetch") or {}).get(env)


def _known_key_names() -> list[str]:
    """Every ENV_VAR name config.yaml actually uses for a key. This is what
    `key clear --all` / `wipe` iterate; it reflects THIS config, not a
    hardcoded list, so it stays correct for whatever is set up.

    Two sources, not one (R150a):
    - each provider's `api_key_env`
    - each `mcp_servers[].env` VALUE — `mcp._resolve_env` resolves those
      through `keystore.get_key()`, so a `GITHUB_TOKEN` for an MCP server
      lives in the same `"aurora-agent"` keyring service as any provider
      key. Missing them meant `wipe` deleted AURORA_HOME while those
      credentials survived in the keyring — exactly the "silently
      un-logging-out the user" hole ARCHITECTURE.md §5 says this function
      exists to close. The `env` MAPPING is {CHILD_VAR: AURORA_KEYSTORE_VAR},
      so it is the values that name keystore entries, never the keys."""
    cfg = _load_raw_config()
    names = {p.get("api_key_env") for p in (cfg.get("providers") or {}).values()
             if p.get("api_key_env")}
    for server in (cfg.get("mcp_servers") or []):
        if isinstance(server, dict):
            names.update(v for v in (server.get("env") or {}).values() if v)
    return sorted(n for n in names if n)


def _key_set(argv: list[str]) -> None:
    import getpass
    import subprocess

    from . import keystore

    env = argv[0] if argv else "LLAMA_API_KEY"
    val = ""
    cmd = _fetch_command(env)
    if cmd:
        print(f"fetch {env} by running:\n  {cmd}")
        from . import ui
        if ui.confirm("Run the fetch command?", default_yes=False):
            # capture stdout only — stderr/stdin stay on the terminal so ssh
            # can do its own host-key / password interaction
            r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                               text=True, timeout=120)
            val = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
            if not val:
                print("fetch produced no output — falling back to manual entry")
    if not val:
        val = getpass.getpass(f"{env} (input hidden): ").strip()
    if not val:
        sys.exit("nothing entered")
    try:                                   # R201: see ui._prompt_and_store_key
        where = keystore.store_key(env, val)
    except keystore.KeystoreError as e:
        sys.exit(f"not stored — {e}")
    print(f"stored {env} in {where}")


def _report_clear(name: str) -> None:
    import os

    from . import keystore
    removed = keystore.clear_key(name)
    where = f"cleared from {', '.join(removed)}" if removed else "not stored (keyring/encrypted file)"
    env_note = " — also set via env var; unset that in your shell yourself" \
        if os.environ.get(name) else ""
    print(f"{name}: {where}{env_note}")


def _key_status(argv: list[str]) -> None:
    from . import keystore
    names = [argv[0]] if argv else _known_key_names()
    if not names:
        print("no api_key_env/token_env configured in config.yaml")
        return
    for name in names:
        print(f"{name}: {keystore.key_status(name)}")


def _key_clear(argv: list[str]) -> None:
    if argv and argv[0] == "--all":
        names = _known_key_names()
        if not names:
            print("no api_key_env/token_env configured in config.yaml")
            return
        for name in names:
            _report_clear(name)
        return
    if not argv:
        sys.exit("usage: aurora key clear <ENV_VAR> | aurora key clear --all")
    _report_clear(argv[0])


def _wipe() -> None:
    from .paths import aurora_home
    home = aurora_home()
    print(f"This deletes {home} — sessions, allowlist, stored keys "
          "(encrypted file), bootstrap prompt, and last-model state.")
    if input("Type 'yes' to confirm: ").strip().lower() != "yes":
        print("cancelled")
        return
    for name in _known_key_names():   # keyring entries live OUTSIDE AURORA_HOME
        _report_clear(name)
    import shutil
    shutil.rmtree(home, ignore_errors=True)
    print(f"→ wiped {home}. Run 'aurora' to start fresh, or ./install.sh "
          "to also pick a new data dir.")


def main() -> None:
    argv = sys.argv[1:]
    if argv in (["--version"], ["-V"]):        # R393: what a bug report or SECURITY.md asks for
        from . import __version__
        print(f"aurora {__version__}")
        return
    if argv[:2] == ["key", "set"] or argv[:1] == ["set"]:  # `set` = common typo
        _key_set(argv[2:] if argv[0] == "key" else argv[1:])
        return
    if argv[:2] == ["key", "status"]:
        _key_status(argv[2:])
        return
    if argv[:2] == ["key", "clear"]:
        _key_clear(argv[2:])
        return
    if argv[:1] == ["wipe"]:
        _wipe()
        return
    if any(a in ("--man", "--help", "-h") for a in argv):
        from .man import man_page
        text = man_page()
        if sys.stdout.isatty():   # pretty ANSI (bold/bullets/code) on a tty
            from .mdrender import LineRenderer
            r = LineRenderer()
            text = "\n".join(r.render(l) for l in text.splitlines())
        print(text)
        return

    # R311: scripts read a headless run's exit code and stdout, so a usage or
    # config error exits 2 (1 means "task incomplete") and the `·` status
    # lines go to stderr, leaving stdout to the answer alone.
    headless_run = "-t" in argv or "--task" in argv

    def die(msg: str) -> NoReturn:
        if headless_run:
            print(msg, file=sys.stderr)
            sys.exit(2)
        sys.exit(msg)

    def info(msg: str) -> None:
        print(msg, file=sys.stderr if headless_run else sys.stdout)

    resume = "--continue" in argv
    resume_id = None
    if "--resume" in argv:
        i = argv.index("--resume")
        try:
            resume_id = argv[i + 1]
        except IndexError:
            die("usage: aurora --resume <session_id>")
        argv = argv[:i] + argv[i + 2:]
        resume = False
    task = None
    for flag in ("-t", "--task"):
        if flag in argv:
            i = argv.index(flag)
            try:
                task = argv[i + 1]
            except IndexError:
                die("usage: aurora -t <task|-> [--yes] [--max-steps N]")
            argv = argv[:i] + argv[i + 2:]
            break
    max_steps = None
    if "--max-steps" in argv:
        i = argv.index("--max-steps")
        try:
            max_steps = int(argv[i + 1])
            if max_steps < 1:
                raise ValueError
        except (IndexError, ValueError):
            die("usage: --max-steps needs a positive integer")
        argv = argv[:i] + argv[i + 2:]
    yes = "--yes" in argv
    as_json = "--json" in argv
    if task == "-":
        task = sys.stdin.read()
    if task is not None and not task.strip():
        die("aurora -t: empty task")
    if task is None and (yes or as_json or max_steps is not None):
        die("--yes, --json and --max-steps only apply with -t/--task")
    web = "--web" in argv
    web_opts = {"--bind": "127.0.0.1", "--port": "9592", "--workspace": "."}   # R393: the current directory, no host-specific default
    for flag in web_opts:
        if flag in argv:
            i = argv.index(flag)
            try:
                web_opts[flag] = argv[i + 1]
            except IndexError:
                die(f"usage: {flag} needs a value")
            argv = argv[:i] + argv[i + 2:]
    if not web and any(f in sys.argv for f in web_opts):
        die("--bind, --port and --workspace only apply with --web")
    classic = "--classic" in argv
    debug = "--debug" in argv
    argv = [a for a in argv if a not in ("--continue", "--classic", "--debug",
                                         "--yes", "--json", "--web")]
    config = argv[0] if argv else _default_config()
    if not Path(config).is_file():
        die(f"not a config file: {config}\n"
                 "usage: aurora [--continue] [--resume ID] [--classic] [--debug] [--man] [config.yaml]\n"
                 "       aurora key set [ENV_VAR]")

    from .engine import Engine
    from .paths import AuroraHomeError

    # R216: config.yaml is hand-edited — R199 and R202 both turned on that
    # fact — so a YAML typo is a normal event, not a corruption scenario. It
    # used to surface as a raw `yaml.parser.ParserError` traceback. The
    # allowlist has had `ApproveLoadError` for this since R170a; the config
    # itself, which is the file users edit far more often, had nothing.
    # The parser's own message already names the file, line and column, so it
    # is worth keeping — it is the traceback around it that is noise.
    try:
        engine = Engine(config)
    except yaml.YAMLError as e:
        die(f"{config}: not valid YAML\n{e}")
    except AuroraHomeError as e:      # R217: an environment mistake, not a bug
        die(str(e))
    for warning in engine.extension_warnings:
        info(f"· {warning}")
    if resume_id:
        # R248: a typo'd id used to print "resuming session <id> (0 turns)"
        # and then run in a BRAND-NEW session — `resume_from` only adopts the
        # past session when it restored something, so the log the user thinks
        # they are continuing is never opened and the turns they then type
        # land somewhere else. `/context` has refused an unknown id since
        # R134; the CLI flag that does the same lookup never did.
        if not sessions.Session(resume_id).log_path.exists():
            die(f"no session {resume_id} on this machine "
                     "(run aurora and use /resume to list them)")
        n = engine.resume_from(resume_id)
        info(f"· resuming session {resume_id} ({n} turns)")
    elif resume:
        last = sessions.latest_session_id()
        if last:
            n = engine.resume_from(last)
            info(f"· continuing session {last} ({n} turns)")
        else:
            info("· no previous session — starting fresh")

    if task is not None:                                       # R311
        from . import headless
        sys.exit(headless.run(engine, task, yes=yes, max_steps=max_steps,
                                     as_json=as_json))

    if web:                                                    # R353
        from .web.server import serve
        try:
            port = int(web_opts["--port"])
        except ValueError:
            die("--port needs an integer")
        serve(engine, web_opts["--bind"], port, web_opts["--workspace"])
        return

    # R578: the terminal never starts on a model nobody picked here. With no remembered model it starts with
    # none ("no model selected — /model to pick one"), instead of the config's first entry, which may be a
    # server this machine does not run (it then failed every message after three connection retries).
    if not engine.model_restored and not engine.messages and len(engine.models) > 1 \
            and sys.stdin.isatty() and sys.stdout.isatty():   # one model: nothing to pick
        engine.current = {}       # a pipe or CI keeps the default, like -t

    # full-screen TUI (pinned prompt, scrollable chat) on a real terminal;
    # --classic or a non-tty (pipes, CI) falls back to the inline REPL
    if classic or not sys.stdout.isatty():
        from . import ui
        ui.run(engine)
    else:
        from . import tui
        tui.run(engine, debug=debug)


if __name__ == "__main__":
    main()
