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

    def notify(self, message: str) -> None:
        """An out-of-band notice (degrade, interrupt, allowlist add, error)."""

    # ── prompts (block until the human answers) ─────────────────────────
    def approve(self, tool: str, args: dict, diff: str):
        """Gate a write/command. Return 'y' (once), 'n' (deny), 'a' (always),
        's' (stop the whole turn), 'c' (don't run; steer the model) — or a
        (key, note) tuple where the note is a denial reason / 'c' guidance
        fed back to the model in the tool result."""

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
        """Polled during work — True once the human hit interrupt (Ctrl+C)."""
