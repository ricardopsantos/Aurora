"""The agent loop: model ⇄ tools until a final answer (R6/R9). Handles the
iteration cap (ask-to-continue at max_iterations), Ctrl+C cancellation (R17),
the approval gate (R7/R8), and malformed-local-tool-call degrade (R5).

UI-agnostic: the caller passes callbacks so this works under any front end.
"""

from dataclasses import dataclass, field
from typing import Callable

from . import approve, tools
from . import secrets as secretscan
from .providers.base import MalformedToolCall, ProviderError, TurnResult


def _provider_label(provider) -> str:
    """A generic, non-identifying descriptor for user-facing connectivity
    messages — NEVER the raw config-key name (`provider.name`, e.g. a
    Tailscale/LAN entry a user may have keyed with something personal) and
    NEVER a local base_url (a MagicDNS/LAN hostname can bake in a personal
    name too). Config is user data; Aurora never echoes it back verbatim."""
    base = getattr(provider, "base_url", "")
    if not base:
        return "the API"
    try:
        from .providers.openai_compat import _is_lan_host
        if _is_lan_host(base):
            return "local backend"
    except Exception:
        pass
    from urllib.parse import urlparse
    return urlparse(base).hostname or "remote backend"   # public SaaS domains are fine to show


def _connectivity_hint(provider) -> str:
    """A command the user can run to test the same connection themselves —
    concrete for a public remote host (its domain isn't personal), generic
    (no hostname) for a local/LAN one."""
    base = getattr(provider, "base_url", "")
    if not base:
        return ""
    try:
        from .providers.openai_compat import _is_lan_host
        if _is_lan_host(base):
            return ("test it yourself: curl -m 5 <your provider's base_url "
                    "from config.yaml>/health")
    except Exception:
        pass
    return (f"test it yourself: curl -m 5 -o /dev/null -s -w '%{{http_code}}\\n' "
            f"{base}")


@dataclass
class AgentCallbacks:
    on_text: Callable[[str], None]                 # streamed assistant text
    on_tool_start: Callable[[str, dict], None]     # tool name, args (pre-run)
    on_tool_result: Callable[[str, str], None]     # tool name, output
    approve: Callable[[str, dict, str], object]    # -> 'y'|'n'|'a'|'s'|'c' or (key, note)
    ask_continue: Callable[[int], object]          # -> bool or (bool, guidance)
    notify: Callable[[str], None]                  # notices (degrade, cancel)
    cancelled: Callable[[], bool]                  # poll for Ctrl+C
    checkpoint: Callable[[str], object] | None = None  # pre-mutation snapshot (R47)
    on_request: Callable[[], None] | None = None   # an LLM request is starting
    # R58: secret-redaction challenge. None means the feature is OFF (the
    # engine only ever sets this when runtime.redact_secrets is true) — the
    # agent loop then skips scanning entirely, no cost when disabled.
    # (context_label, matches) -> 'keep' | 'stop' | 'redact'
    secret_challenge: Callable[[str, list], object] | None = None
    # R58: hashes (secrets.hash_value) of confirmed false positives — matches
    # against these are dropped before secret_challenge ever fires again
    secret_allowlist: set | None = None
    on_usage: Callable[[int, int], None] | None = None


def _norm(ans, default_note: str = "") -> tuple:
    """Callbacks may return a bare value (legacy/simple UIs) or (value, note)."""
    if isinstance(ans, tuple):
        return ans[0], (ans[1] or default_note)
    return ans, default_note


@dataclass
class Turn:
    """Outcome of one user turn, for logging + token accounting."""
    input_tokens: int = 0     # last request's prompt size (context gauge)
    billed_input: int = 0     # SUM of prompt tokens across iterations (cost)
    output_tokens: int = 0    # SUM of completions across iterations (cost)
    # last request's completion size. What actually still occupies the
    # context window is the LAST prompt plus the LAST reply — every earlier
    # round's output is already counted inside the next round's prompt, so
    # summing them into the gauge double-counts and overstates usage on any
    # multi-tool turn (R90d). Cost keeps using the sums above.
    last_output_tokens: int = 0
    # R91: SUM of prompt tokens the provider served from its cache across the
    # turn's iterations — the visible payoff of the cache breakpoint, shown by
    # /cost. Not subtracted from billed_input: providers price a cache read
    # cheaper but not free, and the discount isn't reported uniformly, so the
    # cost estimate stays a deliberate UPPER bound.
    cached_input: int = 0
    iterations: int = 0
    degraded: bool = False
    events: list = field(default_factory=list)


def run_turn(provider, model, messages, system, cb: AgentCallbacks,
             max_iterations: int, tools_enabled: bool, web: bool) -> Turn:
    """Drive one turn. `messages` is mutated in place with the full exchange
    (assistant + tool-result messages) so history persists across turns."""
    turn = Turn()
    allow = approve.load()
    tool_specs = tools.specs(web) if tools_enabled else None
    iteration = 0
    checkpoint = max_iterations   # "continue?" grants another full block
    silent_continue = False       # user picked "keep going, don't ask again"
    last_calls: set[tuple] = set()  # loop detection: calls seen last round

    while True:
        if cb.cancelled():
            cb.notify("interrupted")
            return turn
        iteration += 1
        turn.iterations = iteration
        if cb.on_request:
            cb.on_request()
        try:
            result = provider.turn(model, messages, system, tool_specs,
                                   cb.on_text, cb.cancelled)
        except MalformedToolCall as e:
            # R5: one corrective retry, then degrade to chat. The corrective
            # nudge is a transient message — removed whether the retry
            # succeeds or not, so it never pollutes history (and never leaves
            # two consecutive user turns, which most chat APIs reject).
            if tool_specs is not None and not turn.degraded:
                cb.notify(f"local model emitted a malformed tool call ({e}); retrying once")
                messages.append({"role": "user",
                                 "content": "Your previous tool call was malformed. "
                                            "Emit a single valid tool call, or answer in plain text."})
                if cb.on_request:
                    cb.on_request()
                try:
                    result = provider.turn(model, messages, system, tool_specs,
                                           cb.on_text, cb.cancelled)
                except MalformedToolCall:
                    cb.notify("still malformed — dropping tools for this session (chat only)")
                    turn.degraded = True
                    tool_specs = None
                    continue
                finally:
                    # drop the transient nudge on EVERY retry outcome — a
                    # ProviderError raised here would otherwise leave it in
                    # history as a stray consecutive user message
                    messages.pop()
            else:
                raise
        except ProviderError as e:
            msg = str(e)
            if "exceed" in msg and "context" in msg:
                cb.notify(f"provider error: {e}")
                cb.notify("context is full — /compact to summarize and continue, "
                          "or /clear to start fresh")
            elif "429" in msg or ("rate" in msg.lower() and "limit" in msg.lower()):
                # a free-tier model (e.g. an OpenRouter ":free" variant) shares
                # a rate-limited pool across everyone using it without their
                # own key on that specific upstream — not a bug, an expected
                # limit. The raw error is a wall of provider JSON; give the
                # actionable summary instead.
                cb.notify("rate-limited by the provider — this model's free "
                          "tier is shared; try again shortly, add your own "
                          "provider key to get your own limit, or /model to "
                          "switch to another backend")
            elif any(s in msg.lower() for s in
                     ("timed out", "connect", "unreachable", "refused")):
                # NEVER echo the raw exception here: it embeds the provider's
                # config-key name (e.g. `{self.name} request failed: ...` —
                # a user may key their private server with something personal).
                # Classify local vs remote instead — the connectivity problem
                # can be on ANY provider, so don't assume it was the local one.
                label = _provider_label(provider)
                hint = _connectivity_hint(provider)
                cb.notify(f"{label} unreachable — check your connection, or "
                          "/model to switch to another backend"
                          + (f"\n  {hint}" if hint else ""))
            else:
                cb.notify(f"provider error: {e}")
            turn.events.append({"error": str(e)})
            return turn

        turn.input_tokens = result.input_tokens or turn.input_tokens
        turn.billed_input += result.input_tokens  # every iteration is billed
        turn.output_tokens += result.output_tokens
        turn.last_output_tokens = result.output_tokens or turn.last_output_tokens
        turn.cached_input += getattr(result, "cached_input_tokens", 0) or 0
        if cb.on_usage is not None:
            cb.on_usage(result.input_tokens, result.output_tokens)

        if result.stop_reason == "cancelled":
            cb.notify("interrupted")
            return turn

        messages.append(provider.assistant_message(result))

        if not result.tool_calls:
            return turn  # final answer

        # A round's results are flushed as ONE unit through a provider's
        # optional tool_results_messages() hook (bulk API), if it has one —
        # otherwise one message per result via tool_result_message().
        round_out: list[tuple] = []  # (ToolCall, output)

        def _flush() -> None:
            fn = getattr(provider, "tool_results_messages", None)
            if fn is not None:
                messages.extend(fn(round_out))
            else:
                for c, o in round_out:
                    messages.append(provider.tool_result_message(c, o))
            round_out.clear()

        # iteration cap — each "continue" grants another max_iterations block
        guidance = ""  # optional user steer, injected into this round's results
        if not silent_continue and iteration >= checkpoint:
            go_on, guidance = _norm(cb.ask_continue(iteration))
            if not go_on:
                cb.notify("stopped at iteration cap")
                # feed a synthetic result so history stays valid
                round_out.extend((c, "[skipped: user stopped at the iteration cap]")
                                 for c in result.tool_calls)
                _flush()
                return turn
            if go_on == "silent":
                # user chose to keep going and not be asked again this turn
                silent_continue = True
                cb.notify("continuing — won't ask again this turn")
            else:
                checkpoint = iteration + max_iterations

        this_round = {(c.name, repr(sorted(c.arguments.items())))
                      for c in result.tool_calls}
        repeated = this_round & last_calls
        last_calls = this_round

        # R94: a round's read-only calls (reads/greps/fetches — no approval,
        # no shared state, see tools.PARALLEL_SAFE) are independent, so run
        # them CONCURRENTLY here and consume the results in order below. The
        # model asked for all of them in one message; making it wait for four
        # sequential 2s web fetches is latency nobody chose. Everything
        # user-facing — tool starts, approvals, secret challenges, the
        # transcript, history order — stays strictly sequential.
        # Caveat, accepted: a later "stop"/deny/cancel means some reads in
        # this batch already ran. They have no side effects, so the only cost
        # is work thrown away; their results are still answered `[skipped: …]`
        # so history stays valid. The cap ask (above) runs BEFORE this, so
        # stopping there prefetches nothing.
        prefetched: dict[int, str] = {}
        if tools.PARALLEL_ENABLED and not cb.cancelled():
            batch = [(i, c.name, c.arguments)
                     for i, c in enumerate(result.tool_calls)
                     if c.name in tools.PARALLEL_SAFE]
            if len(batch) > 1:
                for i, name, args in batch:
                    cb.on_tool_start(name, args)   # announce before running
                prefetched = tools.run_tools_parallel(batch)

        for idx, call in enumerate(result.tool_calls):
            def _finish(out: str) -> None:
                # user guidance rides on the round's last tool result so the
                # model reads it before its next step
                if guidance and idx == len(result.tool_calls) - 1:
                    out += f"\n[user guidance: {guidance}]"
                cb.on_tool_result(call.name, out)
                round_out.append((call, out))

            if cb.cancelled():
                cb.notify("interrupted")
                # keep history valid: answer the remaining calls as skipped
                round_out.extend((c, "[skipped: interrupted]")
                                 for c in result.tool_calls[idx:])
                _flush()
                return turn
            if call.name in tools.NEEDS_APPROVAL and not approve.is_allowed(
                    call.name, call.arguments, allow):
                diff = approve.diff_preview(call.name, call.arguments)
                ans, note = _norm(cb.approve(call.name, call.arguments, diff))
                if ans == "a":
                    rule = approve.add_rule(call.name, call.arguments)
                    cb.notify(f"always-allow added: {call.name} · {rule}")
                    allow = approve.load()
                elif ans == "s":
                    cb.notify("stopped by user")
                    round_out.extend((c, "[skipped: user stopped the turn]")
                                     for c in result.tool_calls[idx:])
                    _flush()
                    return turn
                elif ans == "c":
                    _finish(f"[not run — user guidance: {note}]")
                    turn.events.append({"tool": call.name, "steered": note})
                    continue
                elif ans != "y":
                    _finish(f"[denied by user: {note}]" if note
                            else "[denied by user]")
                    turn.events.append({"tool": call.name, "denied": True})
                    continue
            # R58: run_command's PARAMETERS are the deliberate exception to
            # the keep/redact/stop challenge — the command needs its real
            # argument to actually work (a real key in a curl header, say),
            # so silently altering it would just break it, and blocking would
            # duplicate the approval gate it already went through above. This
            # is a NOTICE only: it never blocks, never touches what runs.
            if call.name == "run_command" and cb.secret_challenge:
                param_matches = secretscan.scan(call.arguments.get("command", ""),
                                                cb.secret_allowlist)
                if param_matches:
                    cb.notify(f"possible secret in this command: "
                             f"{secretscan.preview(param_matches)}")
            # R47: snapshot the tree before any mutation lands (approved or
            # allowlisted) — /rewind restores to this point
            if call.name in tools.NEEDS_APPROVAL and cb.checkpoint is not None:
                cb.checkpoint(call.name)
            if idx in prefetched:
                out = prefetched[idx]   # already ran (and announced) in the
                # R94 parallel batch above
            else:
                cb.on_tool_start(call.name, call.arguments)
                out = tools.run_tool(call.name, call.arguments)
            # R58: scan tool output for secrets BEFORE the model (or the
            # display/log, which store the same string) ever sees it — covers
            # every tool uniformly, including read-only ones (read_file, grep)
            # that never went through the approval gate at all.
            if cb.secret_challenge:
                matches = secretscan.scan(out, cb.secret_allowlist)
                if matches:
                    decision = cb.secret_challenge(f"tool:{call.name}", matches,
                                                   source_text=out)
                    if decision == "stop":
                        cb.notify("stopped: secret detected in tool output")
                        round_out.extend(
                            (c, "[skipped: secret detected — user stopped the turn]")
                            for c in result.tool_calls[idx:])
                        _flush()
                        return turn
                    elif decision == "redact":
                        out = secretscan.redact(out, matches)
            # loop nudge: the model just repeated last round's exact call —
            # tell it so instead of silently feeding the same output again
            if (call.name, repr(sorted(call.arguments.items()))) in repeated:
                out += ("\n[note: you already ran this exact call with this "
                        "exact result — do not repeat it; use the result "
                        "above or give your final answer]")
            _finish(out)
            turn.events.append({"tool": call.name, "args": call.arguments})
        _flush()
