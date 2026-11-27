"""R499: run a turn's agent loop in the Rust core (`aurora-rs --core`).

With `runtime.core: rust` set, `Engine.send` keeps everything around a turn
in Python: the prompt's secret check, budgets, compaction before the prompt,
the user and assistant records, costs and the front end. Only the loop moves to
Rust: model requests, tool calls, approvals, checkpoints and extensions. The
front ends (TUI, classic REPL, web page) are unchanged because they talk to the
same `Frontend` callbacks. Protocol `aurora-core/1` (documents/CORE_PROTOCOL.md):
one JSON object per line on the core's stdin and stdout.

On the web page the core starts with `--web` (R500): the path guard (R450) on
and commands boxed unless `sandbox: off` (R475), as Python's web mode does.
The page's own approval rules (web denylist, auto-approve) stay in its
front end, which answers the core's approval requests.

Anything the Rust core cannot do yet runs on the Python loop, with a notice:
plan mode, pictures in the prompt, a model the Rust core cannot open. A core
that dies mid-turn ends that turn with a notice; the next turn starts a new one.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import agent, approve

PROTOCOL = "aurora-core/1"
_POLL_S = 0.1


def binary() -> str | None:
    """`AURORA_RS`, else `aurora-rs` on PATH, else this checkout's build."""
    env = os.environ.get("AURORA_RS")
    if env:
        return env if os.access(env, os.X_OK) else None
    found = shutil.which("aurora-rs")
    if found:
        return found
    root = Path(__file__).resolve().parent.parent
    for kind in ("release", "debug"):
        p = root / "target" / kind / "aurora-rs"
        if os.access(p, os.X_OK):
            return str(p)
    return None


def wanted(engine) -> bool:
    forced = os.environ.get("AURORA_CORE", "").strip().lower()      # tests and the nightly force one core
    if forced in ("python", "rust"):
        return forced == "rust"
    # R508: the Rust loop is the default; `runtime.core: python` keeps the Python one
    return str((engine.runtime or {}).get("core", "rust")).strip().lower() != "python"


def explicit(engine) -> bool:
    """The user asked for the Rust core (config or AURORA_CORE), so a turn kept on Python is worth a notice."""
    if os.environ.get("AURORA_CORE", "").strip().lower() == "rust":
        return True
    return str((engine.runtime or {}).get("core", "")).strip().lower() == "rust"


_PY_RUN_TURN = agent.run_turn
_RUST_ERROR_FOLLOWUPS = ("context is full —", "rate-limited by the provider —")


def unsupported(engine) -> str | None:
    """Why this turn must run on the Python loop, or None."""
    if getattr(engine, "plan_mode", False):
        return "plan mode"
    if any(m.get("images") for m in engine.messages if isinstance(m, dict)):
        return "pictures in the conversation"
    if not getattr(engine, "config_path", None):
        return "no config file"
    if agent.run_turn is not _PY_RUN_TURN:      # a Python loop swapped in (embedding, tests): it must be the one used
        return "the Python loop was replaced in-process"
    if binary() is None:
        return "aurora-rs not found (set AURORA_RS or build it with cargo)"
    return None


class Core:
    """One `aurora-rs --core` process, for one model."""

    def __init__(self, exe: str, config_path: str, model: str, web: bool = False, bridge: bool = False):
        env = dict(os.environ, AURORA_EXT_QUIET="1", AURORA_PYTHON=os.environ.get("AURORA_PYTHON") or sys.executable,
                   PYTHONPATH=os.pathsep.join(p for p in (str(Path(__file__).resolve().parent.parent),
                                                          os.environ.get("PYTHONPATH", "")) if p))
        self.key = (config_path, model, web, bridge)
        # R500: the web page's core runs with the path guard on and commands boxed (unless sandbox: off)
        self.proc = subprocess.Popen([exe, "--core", *(["--web"] if web else []), *(["--provider-bridge"] if bridge else []), "--config", config_path, "--model", model],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                                     text=True, encoding="utf-8", bufsize=1)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        first = self._next(timeout=60)
        if not first or first.get("ev") != "ready" or first.get("protocol") != PROTOCOL:
            self.close()
            raise RuntimeError(f"aurora-rs --core did not start ({first!r})")
        self.tools = first.get("tools", [])

    def _pump(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self.lines.put(json.loads(line))
                except ValueError:
                    pass
        self.lines.put(None)

    def _next(self, timeout=None):
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return {}

    def write(self, obj) -> None:
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        try:
            if self.alive():
                self.write({"op": "shutdown"})
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            self.proc.kill()


def _core_for(engine, model: str) -> Core:
    from . import guard
    core = getattr(engine, "_rust_core", None)
    # R505: a provider object given in-process (embedding, tests) runs in Python, the loop in Rust
    bridge = "_provider_for" in vars(engine)
    exe = binary()
    try:                       # R526: a rebuilt aurora-rs (./install.sh) is picked up at the next turn, not at a restart
        stamp = os.stat(exe).st_mtime_ns
    except (OSError, TypeError):
        stamp = 0
    key = (engine.config_path, model, guard.WEB, bridge)
    if core is None or core.key != key or not core.alive() or getattr(core, "exe", None) != (exe, stamp):
        if core is not None:
            core.close()
        core = Core(exe, engine.config_path, model, web=guard.WEB, bridge=bridge)
        core.exe = (exe, stamp)
        engine._rust_core = core
    return core


def _answer_approve(cb, tool, args, diff):
    while True:
        a = cb.approve(tool, args, diff)
        key = a[0] if isinstance(a, tuple) else a
        if key != "e":
            return list(a) if isinstance(a, tuple) else a
        cb.notify("explanations are not available on the Rust core yet")


def _system_text(system) -> str:
    if isinstance(system, list):        # cache-block form: the text of each block
        return "\n\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)
    return system or ""


def _bridged_request(engine, core, provider, msg, cb) -> dict:
    """R505: run one model request on the Python provider object, streaming its text to the core."""
    from .providers.base import MalformedToolCall, ProviderError

    def on_text(chunk):
        core.write({"op": "provider_chunk", "kind": "text", "chunk": chunk})
    if cb.cancelled():
        return {"result": {"stop_reason": "cancelled"}}
    try:
        r = provider.turn(msg.get("model", ""), msg.get("messages") or [], msg.get("system", ""), msg.get("tools") or None,
                          on_text, cb.cancelled)
    except MalformedToolCall as e:
        return {"error": str(e), "malformed": True}
    except ProviderError as e:
        return {"error": str(e), "malformed": False}
    except BaseException:
        core.close()                                # the core waits on this answer: end it, then let it escape
        engine._rust_core = None
        raise
    return {"result": {"text": r.text or "", "stop_reason": r.stop_reason or "",
                       "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in r.tool_calls],
                       "input_tokens": r.input_tokens or 0, "output_tokens": r.output_tokens or 0,
                       "cached_input_tokens": getattr(r, "cached_input_tokens", 0) or 0,
                       "reasoning_tokens": getattr(r, "reasoning_tokens", 0) or 0,
                       "reasoning_chars": getattr(r, "reasoning_chars", 0) or 0},
            "assistant_message": provider.assistant_message(r)}


def run_turn(engine, provider, model: str, cb, tools_enabled: bool) -> agent.Turn:
    """`agent.run_turn`'s contract: grow `engine.messages`, drive `cb`, return a Turn."""
    core = _core_for(engine, model)
    sent = list(engine.messages)
    prompt = next((m.get("content", "") for m in reversed(sent) if m.get("role") == "user"), "")
    rt = engine.runtime or {}
    core.write({
        "op": "turn", "messages": sent, "prompt": prompt if isinstance(prompt, str) else "",
        # R506: the system prompt Python composed (notes, skills, …): the model sees the same one on both cores
        "system": _system_text(engine.system),
        "tools": bool(tools_enabled),
        "session_rules": approve.session_rules(),
        **({"secret_allowlist": sorted(cb.secret_allowlist)} if cb.secret_allowlist is not None else {}),
        **({"secret_session_keep": sorted(cb.secret_session_keep)} if cb.secret_session_keep is not None else {}),
        "auto_approve": bool(cb.auto_approve and cb.auto_approve()),
        "max_iterations": engine.max_iterations,
        "compaction": {"auto": bool(getattr(engine, "auto_compact", True)),
                       "threshold_pct": float(getattr(engine, "auto_compact_threshold_pct", 80)),
                       "keep_recent_tokens": int(getattr(engine, "compact_keep_recent_tokens", 20000)),
                       "context_limit": int(engine.context_stats().limit or 0),
                       "used": int(getattr(engine, "_used", 0) or 0),
                       "count": int(getattr(engine, "compactions", 0) or 0)},
    })
    turn = agent.Turn()
    turn_start = time.time()
    rendered_error = False
    cancel_sent = False
    if cb.cancelled():                              # Python's loop checks before every request: so does this one
        core.write({"op": "cancel"})
        cancel_sent = True
    on_think = getattr(provider, "on_think", None)
    while True:
        msg = core._next(timeout=_POLL_S)
        if msg == {}:
            if not cancel_sent and cb.cancelled():
                core.write({"op": "cancel"})
                cancel_sent = True
            continue
        if msg is None:
            cb.notify("the Rust core stopped mid-turn — this turn ended; the next one starts a new core")
            engine._rust_core = None
            turn.cancelled = True
            return turn
        ev = msg.get("ev")
        if ev == "event":
            kind = msg.get("type")
            if kind == "text":
                cb.on_text(msg.get("chunk", ""))
                turn.produced = True
            elif kind == "think":
                if on_think:
                    on_think(msg.get("chunk", ""))
            elif kind == "tool_start":
                cb.on_tool_start(msg.get("name"), msg.get("args") or {})
            elif kind == "tool_result":
                cb.call_args = msg.get("args") or {}
                cb.on_tool_result(msg.get("name"), msg.get("output", ""))
                turn.produced = True
            elif kind == "usage":
                if cb.on_usage is not None:
                    cb.on_usage(msg.get("input_tokens", 0), msg.get("output_tokens", 0),
                                msg.get("cached_input_tokens", 0))
            elif kind == "notice":
                text = msg.get("message", "")
                if text.startswith("provider error: "):      # R504: Python's wording, never the raw config-key name
                    rendered_error = True
                    for line in agent.provider_error_notices(provider, text[len("provider error: "):]):
                        cb.notify(line)
                elif rendered_error and text.startswith(_RUST_ERROR_FOLLOWUPS):
                    pass                                        # already said by provider_error_notices
                else:
                    cb.notify(text)
        elif ev == "log":
            # tool records are written by cb.on_tool_result, as on the Python loop
            data = msg.get("data") or {}
            if msg.get("event") == "approval" and cb.on_approval is not None:     # the engine's own writes this record
                try:                    # R133c: a logging callback never kills the turn
                    cb.on_approval(data.get("tool", ""), data.get("decision", ""), data.get("detail", ""))
                except Exception:
                    pass
            elif msg.get("event") != "tool":
                engine.session.log(msg.get("event"), **data)
        elif ev == "provider":
            core.write({"op": "answer", "id": msg["id"], "value": _bridged_request(engine, core, provider, msg, cb)})
        elif ev == "approve":
            core.write({"op": "answer", "id": msg["id"],
                        "value": _answer_approve(cb, msg.get("tool"), msg.get("args") or {}, msg.get("diff", ""))})
        elif ev == "continue":
            n = msg.get("iterations", 0)
            if (cb.auto_continue and cb.auto_continue() and n < agent.AUTO_CONTINUE_MAX_ROUNDS
                    and time.time() - turn_start < agent.AUTO_CONTINUE_MAX_SECS):
                value = True        # R314: one more block without asking, up to the backstops; then the prompt returns
            else:
                a = cb.ask_continue(msg.get("iterations", 0))
                value = list(a) if isinstance(a, tuple) else bool(a)
            core.write({"op": "answer", "id": msg["id"], "value": value})
        elif ev == "budget":                        # the spend limit, checked by the front end that keeps the costs
            state = cb.budget() if cb.budget else ("ok", "")
            core.write({"op": "answer", "id": msg["id"], "value": list(state)})
        elif ev == "secret":
            sc = cb.secret_challenge
            value = sc(msg.get("context", ""), [], source_text=msg.get("summary", "")) if sc else "stop"
            core.write({"op": "answer", "id": msg["id"], "value": value})
        elif ev == "done":
            out, t = msg.get("messages") or [], msg.get("turn") or {}
            if len(out) >= len(sent) and out[:len(sent)] == sent:
                engine.messages.extend(out[len(sent):])
            else:                                   # folded mid-turn: history was rewritten
                engine.messages[:] = out
            for k in ("input_tokens", "output_tokens", "billed_input", "cached_input", "reasoning_tokens",
                      "reasoning_chars", "last_request_latency", "iterations", "degraded", "cancelled",
                      "gen_tokens", "gen_secs"):
                if k in t:
                    setattr(turn, k, t[k])
            turn.last_output_tokens = int(t.get("last_output_tokens") or 0)
            for tool, rules in (t.get("session_rules") or {}).items():       # R334: "t" answers given on the core
                for rule in rules:
                    if rule not in approve._SESSION_RULES.setdefault(tool, []):
                        approve._SESSION_RULES[tool].append(rule)
            for k in ("secret_allowlist", "secret_session_keep"):    # what this turn added, back to the front end's sets
                mine = getattr(cb, k)
                if mine is not None and isinstance(t.get(k), list):
                    mine.update(t[k])
            if isinstance(t.get("compactions"), int):
                engine.compactions = t["compactions"]
            turn.produced = turn.produced or len(engine.messages) > len(sent)
            if t.get("auto_approve") and cb.set_auto_approve and not (cb.auto_approve and cb.auto_approve()):
                cb.set_auto_approve(True)               # R348: "o" at a gate
            if t.get("error"):
                turn.events.append({"error": t["error"]})
            typed = [n for n in (cb.steer() if cb.steer else []) if n]
            if typed:                                   # R348 is not in aurora-core/1 v1: say so, never drop them silently
                cb.notify(f"{len(typed)} line{'s' if len(typed) != 1 else ''} typed mid-turn not delivered "
                          f"(the Rust core cannot steer yet): {' / '.join(typed)[:200]}")
            return turn
