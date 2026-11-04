"""R311: one-shot, non-interactive run — `aurora -t "task"`.

Built for cron/CI and for another agent delegating to Aurora. The approval
gate in agent.py already consults the saved allowlist and denylist BEFORE it
asks the front end, so headless only needs a front end that never prompts:
anything not allowlisted is denied unless `--yes` (engine.auto_approve), and
the denylist still wins over `--yes`.

stdout carries only the assistant's text so a script can capture it; progress
goes to stderr. Exit code: 0 finished cleanly, 1 anything went wrong (a gated
call was denied, step cap hit, secret stop, cancelled, provider failure).
"""

import json
import signal
import sys

from . import keystore
from .providers.base import ProviderError

DEFAULT_MAX_STEPS = None     # None = use runtime.max_iterations from config


class HeadlessFrontend:
    def __init__(self, out=None, err=None, yes: bool = False,
                 as_json: bool = False):
        self.as_json = as_json       # R337: collect, print one JSON object at the end
        self.reply: list[str] = []
        self.calls: list[dict] = []
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.yes = yes
        self.failed = False          # any reason the task may be incomplete
        self.reasons: list[str] = []
        self._stop = False
        self._wrote = False
        self._tools = 0

    def _fail(self, why: str) -> None:
        self.failed = True
        self.reasons.append(why)

    def interrupt(self, *_a) -> None:
        self._stop = True

    # ── output ──────────────────────────────────────────────────────────
    def on_text(self, chunk: str) -> None:
        self._wrote = True
        if self.as_json:
            self.reply.append(chunk)
            return
        self.out.write(chunk)
        self.out.flush()

    def on_think(self, chunk: str) -> None:
        pass

    def on_tool_start(self, name: str, args: dict) -> None:
        self._tools += 1
        self.calls.append({"name": name, "args": args})
        first = next(iter(args.values()), "") if args else ""
        self.err.write(f"· {name} {str(first).splitlines()[0][:100] if first else ''}\n")

    def on_tool_result(self, name: str, output: str) -> None:
        # a denylist hit never reaches approve(), so catch it by its result
        if self.calls and "result" not in self.calls[-1]:
            self.calls[-1]["result"] = output[:2000]
        if output.startswith("[denied by policy"):
            self._fail(f"{name} denied by policy")
        self.err.write(f"  → {output.strip().splitlines()[0][:100] if output.strip() else '[no output]'}\n")

    def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        pass

    def notify(self, message: str) -> None:
        self.err.write(f"· {message}\n")
        if message.startswith(("stopped", "interrupted", "provider error")):
            self._fail(message)

    def invalidate_status(self) -> None:
        pass

    # ── prompts: never block on a human ─────────────────────────────────
    def approve(self, tool: str, args: dict, diff: str):
        if self.yes:
            return "y"
        self._fail(f"{tool} denied (not on the allowlist)")
        return ("n", "headless: not on the allowlist — ask the user to allow it "
                     "or run with --yes in a disposable environment")

    def ask_continue(self, iterations: int):
        self._fail(f"step cap reached after {iterations} iterations")
        return False

    def ask_secret(self, label: str) -> str:
        return ""

    def secret_challenge(self, context: str, matches: list,
                         source_text: str = "") -> str:
        self._fail(f"likely secret in {context}")
        return "stop"

    def cancelled(self) -> bool:
        return self._stop


def _report(engine, fe: "HeadlessFrontend", code: int) -> dict:
    """R337: the machine-readable result of a run."""
    try:
        st = engine.context_stats()
        usage = {"cost_usd": round(float(st.cost_usd), 6) if st.cost_known else None,
                 "context_tokens": st.used}
    except Exception:
        usage = {"cost_usd": None, "context_tokens": None}
    return {"ok": code == 0, "exit_code": code,
            "reply": "".join(fe.reply),
            "reasons": fe.reasons,
            "model": engine.current.get("model"),
            "session": getattr(engine.session, "id", None),
            "tool_calls": fe.calls, **usage}


def run(engine, task: str, *, yes: bool = False,
        max_steps: int | None = None, as_json: bool = False) -> int:
    fe = HeadlessFrontend(yes=yes, as_json=as_json)
    keystore.set_prompter(fe.ask_secret)
    if max_steps is not None:
        engine.max_iterations = max_steps
    engine.auto_approve = yes
    engine.headless = True
    old = signal.signal(signal.SIGINT, fe.interrupt)
    try:
        engine.send(task, fe)
    except ProviderError as e:
        fe._fail(f"provider error: {e}")
        fe.err.write(f"error: {e}\n")
    finally:
        signal.signal(signal.SIGINT, old)
    if not fe._wrote and not fe._tools and not fe.failed:
        # engine.send reports a dead provider via notify and returns
        fe._fail("the model produced no response")
    code = 1 if fe.failed else 0
    if as_json:
        fe.out.write(json.dumps(_report(engine, fe, code), ensure_ascii=False) + "\n")
        fe.out.flush()
    elif fe._wrote:
        fe.out.write("\n")
        fe.out.flush()
    if fe.failed:
        fe.err.write("headless: incomplete — " + "; ".join(fe.reasons) + "\n")
    return code
