"""The engine ⇄ UI contract — the ONLY surface between the two halves.

A front end (terminal today, HTML/websocket tomorrow) implements `Frontend`.
The engine calls these methods; it never imports a concrete UI, never touches
stdin/stdout, never renders. Swap the UI by writing a new Frontend; the engine
is untouched. Improve the engine freely as long as this interface holds.

Everything the engine needs from a human — stream a chunk, show a tool run,
ask approval, ask a passphrase — is a method here. If the engine ever needs a
new kind of interaction, it goes here first, then every UI implements it.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Frontend(Protocol):
    # ── streaming output ────────────────────────────────────────────────
    def on_text(self, chunk: str) -> None:
        """A chunk of streamed assistant text."""

    def on_think(self, chunk: str) -> None:
        """A chunk of a thinking model's reasoning stream. Display-only —
        it is never part of the answer text or the history."""

    def on_tool_start(self, name: str, args: dict) -> None:
        """A tool is about to run (already approved)."""

    def on_tool_result(self, name: str, output: str) -> None:
        """A tool finished; `output` is what the model will see."""

    def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Provider-reported token counts for the current model request.
        Called once per request/iteration after the stream finishes.
        Both arguments may be 0 when the provider did not report usage."""

    def notify(self, message: str) -> None:
        """An out-of-band notice (degrade, interrupt, allowlist add, error)."""

    def invalidate_status(self) -> None:
        """R154: engine state the status display derives from has changed —
        redraw it. Called mid-turn (after each tool result, whose size the
        context gauge now counts immediately), so it must be cheap and safe
        from the worker thread. A front end with nothing to redraw
        implements it as a no-op."""

    # ── prompts (block until the human answers) ─────────────────────────
    def approve(self, tool: str, args: dict, diff: str):
        """Gate a write/command. Return 'y' (once), 'n' (deny), 'a' (always),
        's' (stop the whole turn), 'c' (don't run; steer the model), or 'e'
        (R103: get a model-written explanation of the call, then re-ask this
        SAME challenge — 'e' is never a terminal answer, the caller loops on
        it) — or a (key, note) tuple where the note is a denial reason / 'c'
        guidance fed back to the model in the tool result."""

    def ask_continue(self, iterations: int):
        """Tool loop hit the cap after `iterations` — keep going? Return a
        bool, ('silent', '') to keep going and not be asked again this turn,
        or (True, guidance) to continue with a steer for the model."""

    def ask_secret(self, label: str) -> str:
        """A hidden-input prompt (API key, key-store passphrase). '' = skip."""

    def secret_challenge(self, context: str, matches: list,
                         source_text: str = "") -> str:
        """R58: a likely secret was found in `context` ('prompt' or
        'tool:<name>'). `matches` is a list of secrets.Match. Return 'keep'
        (send/log as-is), 'stop' (abort), 'redact' (replace each match with
        <secret> before it's sent/logged), or 'always' (allowlist every
        matched value so it's never flagged again, then keep as-is —
        the engine handles persisting this, callers just get 'keep' back).
        `source_text` is the original prompt or tool output so the UI can
        show each match in context."""

    # ── control ─────────────────────────────────────────────────────────
    def cancelled(self) -> bool:
        """Polled during work — True once the human asked to cancel.

        Which key that is belongs to the front end, not to this contract:
        the TUI cancels a busy turn on Esc (Ctrl+C there only clears the
        input line), the classic REPL on Ctrl+C. Naming one of them here
        made the shared contract read as if the TUI's Ctrl+C cancelled,
        which it has not since Esc became the single control key."""
