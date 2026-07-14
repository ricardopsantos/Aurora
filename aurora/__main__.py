"""Entry point: `aurora` | `python -m aurora`.

  aurora                     start (auto-detects .agentic_context/ in the cwd)
  aurora --continue          resume the most recent session
  aurora --resume ID         resume a specific session
  aurora --classic           inline REPL instead of the full-screen TUI
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

import sys
from pathlib import Path

from . import session as sessions


def _default_config() -> str:
    """config.yaml next to the package (repo checkout), else AURORA_HOME."""
    repo = Path(__file__).resolve().parent.parent / "config.yaml"
    if repo.is_file():
        return str(repo)
    from .paths import aurora_home
    home_cfg = aurora_home() / "config.yaml"
    if home_cfg.is_file():
        return str(home_cfg)
    sys.exit("no config.yaml found (repo root or AURORA_HOME)")


def _load_raw_config() -> dict:
    try:
        import yaml
        return yaml.safe_load(open(_default_config())) or {}
    except Exception:
        return {}


def _fetch_command(env: str) -> str | None:
    """config.yaml key_fetch: <ENV_VAR> → shell command that prints the value
    (e.g. an ssh to wherever it lives). The command is shown and only runs on
    explicit approval."""
    return (_load_raw_config().get("key_fetch") or {}).get(env)


def _known_key_names() -> list[str]:
    """Every ENV_VAR name config.yaml actually uses for a key — each
    provider's api_key_env plus llamadesk's token_env, if configured. This is
    what `key clear --all` / `wipe` iterate; it reflects THIS config, not a
    hardcoded list, so it stays correct for whatever providers are set up."""
    cfg = _load_raw_config()
    names = {p.get("api_key_env") for p in (cfg.get("providers") or {}).values()
             if p.get("api_key_env")}
    token_env = (cfg.get("llamadesk") or {}).get("token_env")
    if token_env:
        names.add(token_env)
    return sorted(names)


def _key_set(argv: list[str]) -> None:
    import getpass
    import subprocess
    from . import keystore

    env = argv[0] if argv else "ANTHROPIC_API_KEY"
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
    where = keystore.store_key(env, val)
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

    resume = "--continue" in argv
    resume_id = None
    if "--resume" in argv:
        i = argv.index("--resume")
        try:
            resume_id = argv[i + 1]
        except IndexError:
            sys.exit("usage: aurora --resume <session_id>")
        argv = argv[:i] + argv[i + 2:]
        resume = False
    classic = "--classic" in argv
    debug = "--debug" in argv
    argv = [a for a in argv if a not in ("--continue", "--classic", "--debug")]
    config = argv[0] if argv else _default_config()
    if not Path(config).is_file():
        sys.exit(f"not a config file: {config}\n"
                 "usage: aurora [--continue] [--resume ID] [--classic] [--debug] [--man] [config.yaml]\n"
                 "       aurora key set [ENV_VAR]")

    from .engine import Engine

    engine = Engine(config)
    if resume_id:
        n = engine.resume_from(resume_id)
        print(f"· resuming session {resume_id} ({n} turns)")
    elif resume:
        last = sessions.latest_session_id()
        if last:
            n = engine.resume_from(last)
            print(f"· continuing session {last} ({n} turns)")
        else:
            print("· no previous session — starting fresh")

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
