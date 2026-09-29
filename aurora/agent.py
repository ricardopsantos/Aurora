"""The agent loop: model ⇄ tools until a final answer (R6/R9). Handles the
iteration cap (ask-to-continue at max_iterations), user cancellation (R17),
the approval gate (R7/R8), and malformed-local-tool-call degrade (R5).

UI-agnostic: the caller passes callbacks so this works under any front end.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import approve, tools
from . import secrets as secretscan
from .providers.base import (
    MalformedToolCall,
    ProviderError,
    side_completion,
)


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


# R58/R131: tools whose own `command` argument is scanned for secrets and
# REPORTED (never blocked, never rewritten) before it runs — the deliberate
# exception to the keep/redact/stop challenge, because a shell command
# usually needs the real value to work and blocking here would duplicate the
# approval gate it already passed. Mirrors approve._COMMAND_TOOLS: the same
# two tools that take a shell `command` string.
_COMMAND_ARG_TOOLS = ("run_command", "wait_until")

# R58 gap fix (2026-07-27): write_file/edit_file/apply_patch arguments were
# never scanned at all — a model copying a secret from an earlier tool
# result into a new file it's writing hit no notice and no challenge, only
# the user's prompt and tool OUTPUT were covered. Maps each tool to the one
# argument that actually carries new content being written — `old`
# (edit_file) is what's already in the file, not new exposure, so it's
# deliberately excluded to avoid a redundant challenge on text the file
# already contains.
_WRITE_ARG_FIELD = {"write_file": "content", "edit_file": "new",
                    "apply_patch": "diff"}


_EXPLAIN_PROMPT = """\
Explain in 2-4 plain-English sentences what this tool call will do and \
why it might be run, for a user who is deciding whether to approve it. \
Be concrete about side effects — what gets written, changed, or executed \
— rather than restating the raw arguments verbatim.

Tool: {name}
Arguments: {args}
"""


def _explain_tool_call(provider, model, name: str, args: dict,
                       cancel=None) -> str:
    """R103: the approval gate's "explain" option. A one-off, tool-free
    model completion describing what a PENDING call will do — never added
    to the conversation history, same "side completion" shape as
    memory._draft()/gitcommit.draft_message(). Deliberately asked of the
    SAME provider/model already selected for the turn: an explanation from
    a different model than the one that chose to make the call would be
    answering for a decision it didn't make."""
    ask = _EXPLAIN_PROMPT.format(name=name, args=json.dumps(args, indent=2))
    try:
        # R255: `side_completion`, not a bare `provider.turn` — see there for
        # why reading `result.text` alone silently discarded the explanation
        # on a thinking model, which is what made this option look broken.
        # R257: the ONE caller that opts into the reasoning fallback —
        # here the reasoning is prose the user reads, not an artifact
        # some later step consumes. See `side_completion`.
        out = side_completion(provider, model, ask, cancel=cancel,
                              reasoning_fallback=True)
        return out or ("(no explanation returned — skip with 'n' and ask "
                       "in chat instead)")
    except Exception as e:
        return f"[explain failed: {e.__class__.__name__}: {e}]"


@dataclass
class AgentCallbacks:
    on_text: Callable[[str], None]                 # streamed assistant text
    on_tool_start: Callable[[str, dict], None]     # tool name, args (pre-run)
    on_tool_result: Callable[[str, str], None]     # tool name, output
    approve: Callable[[str, dict, str], object]    # -> 'y'|'n'|'a'|'s'|'c' or (key, note)
    ask_continue: Callable[[int], object]          # -> bool or (bool, guidance)
    notify: Callable[[str], None]                  # notices (degrade, cancel)
    cancelled: Callable[[], bool]                  # poll for a user cancel (Esc/Ctrl+C)
    checkpoint: Callable[[str, dict], object] | None = None  # pre-mutation snapshot (R47/R181)
    on_request: Callable[[], None] | None = None   # an LLM request is starting
    # R262: `/auto-approve on` — polled fresh per gated call (not read once
    # at turn start) so toggling it mid-turn takes effect on the very next
    # tool call, same as every other live `/command` this loop already
    # reads through a callback instead of a snapshot. None means "no such
    # concept" for a caller that never wires it — treated as always-off.
    auto_approve: Callable[[], bool] | None = None
    # R58: secret-redaction challenge. None means the feature is OFF (the
    # engine only ever sets this when runtime.redact_secrets is true) — the
    # agent loop then skips scanning entirely, no cost when disabled.
    # (context_label, matches) -> 'keep' | 'stop' | 'redact'
    secret_challenge: Callable[[str, list], object] | None = None
    # R58: hashes (secrets.hash_value) of confirmed false positives — matches
    # against these are dropped before secret_challenge ever fires again
    secret_allowlist: set | None = None
    # R192: (input_tokens, output_tokens, cached_input_tokens) per round.
    on_usage: Callable[[int, int, int], None] | None = None
    # R133c: every outcome of the approval gate, including the ones that never
    # ask the user (allowlisted, denied by policy). Wired at the gate itself
    # rather than around `approve` because a daily driver with an allowlist
    # takes the no-question path most of the time — logging only the asked
    # ones would under-report exactly the common case.
    # (tool, decision, detail) — decision is one of _APPROVAL_DECISIONS.
    on_approval: Callable[[str, str, str], None] | None = None
    # R154: "history is at a safe boundary — fold it if it's too big." Called
    # between rounds ONLY (never mid-round), because that is the one point
    # where every assistant `tool_calls` entry already has its matching
    # `tool` results and the list can be rewritten without producing an
    # invalid sequence. The engine owns the policy (threshold, whether it's
    # enabled at all); the loop only owns knowing when it's safe to ask.
    # Must mutate `messages` IN PLACE — see run_turn.
    maybe_compact: Callable[[], None] | None = None


# R133c: the closed set of gate outcomes. Kept as data so the session log and
# any reader agree on the vocabulary instead of matching free text.
_APPROVAL_DECISIONS = ("allowlisted", "approved", "always_allow", "denied",
                       "denied_policy", "always_deny", "steered", "stopped",
                       "auto_approved")


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
    # R133a: SUM of reasoning tokens across iterations — a SUBSET of
    # output_tokens, never added to it. `reasoning_chars` is the streamed
    # fallback for backends that report no reasoning_tokens (local llama.cpp).
    reasoning_tokens: int = 0
    reasoning_chars: int = 0
    # /model picker (feature request, 2026-07-27): wall time of the LAST
    # successful `provider.turn()` call this turn made — a cached, no-probe
    # signal of "how fast is this model responding right now", logged
    # alongside the assistant record so the picker can read it back later
    # without a live network call. Overwritten each round; the final value
    # is whichever round completed last, which is what a "how did this model
    # just do" reading should mean, not the sum/average across a multi-tool
    # turn.
    last_request_latency: float = 0.0
    iterations: int = 0
    # R266: did this turn append ANY assistant message? The engine used to
    # infer it from `len(messages)` growing, which a mid-turn fold (R154)
    # shrinks in place — a productive turn then read as "produced nothing".
    produced: bool = False
    degraded: bool = False
    cancelled: bool = False
    events: list = field(default_factory=list)


def _load_allow_or_empty(cb: AgentCallbacks) -> dict:
    """R196: the allowlist's counterpart to R170a's denylist handling.

    A corrupt `denylist.yaml` is caught and fails CLOSED with a message. A
    corrupt `allowlist.yaml` raised `ApproveLoadError` straight out of the
    agent loop instead — only `ProviderError` is caught around `run_turn`, so
    the whole turn died on a YAML typo in a file Aurora explicitly invites
    the user to hand-edit.

    Empty IS the fail-closed answer for this direction: no rule matches, so
    every gated call is prompted for, which is exactly the pre-allowlist
    behaviour. Failing closed on the deny side means blocking; on the allow
    side it means asking. Both refuse to act on rules they cannot read."""
    try:
        return approve.load()
    except approve.ApproveLoadError as e:
        cb.notify(f"allowlist.yaml is unreadable ({e}) — asking for approval "
                  "on every gated tool call until it's fixed")
        return {}


def run_turn(provider, model, messages, system, cb: AgentCallbacks,
             max_iterations: int, tools_enabled: bool) -> Turn:
    """Drive one turn. `messages` is mutated in place with the full exchange
    (assistant + tool-result messages) so history persists across turns."""
    turn = Turn()
    allow = _load_allow_or_empty(cb)
    try:
        deny = approve.load_deny()   # R120: empty dict for anyone who's never set one
        deny_broken = False
    except approve.ApproveLoadError as e:
        # R170a: a corrupt denylist.yaml must fail CLOSED, not silently
        # behave like an empty one (which would disable deny enforcement
        # with no visible error). Block every gated call until fixed.
        cb.notify(f"denylist.yaml is unreadable ({e}) — blocking all gated "
                  "tool calls until it's fixed")
        deny, deny_broken = {}, True
    tool_specs = tools.specs() if tools_enabled else None
    iteration = 0
    checkpoint = max_iterations   # "continue?" grants another full block
    silent_continue = False       # user picked "keep going, don't ask again"
    # loop detection: (call, output) seen last round — keyed on RESULT too,
    # not just the call, so a legitimate re-run (write → test → fix →
    # re-test) whose output actually changed is never told "you already ran
    # this exact call with this exact result" when it didn't.
    last_results: dict[tuple, str] = {}

    while True:
        if cb.cancelled():
            cb.notify("interrupted")
            turn.cancelled = True
            return turn
        # R154: fold history BEFORE building the next request, not after the
        # turn ends. `iteration` is still the count of COMPLETED rounds here,
        # so this never fires on the first request of a turn (nothing new to
        # fold yet) and always fires at a round boundary, where history is a
        # valid sequence. The callback mutates `messages` in place, so the
        # `provider.turn` below sees the folded list — an engine that rebound
        # its own `self.messages` instead would leave this loop driving the
        # pre-fold list for the rest of the turn.
        if iteration and cb.maybe_compact is not None:
            cb.maybe_compact()
        iteration += 1
        turn.iterations = iteration
        if cb.on_request:
            cb.on_request()
        _t0 = time.monotonic()
        try:
            result = provider.turn(model, messages, system, tool_specs,
                                   cb.on_text, cb.cancelled)
            turn.last_request_latency = time.monotonic() - _t0
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
                _t0 = time.monotonic()
                try:
                    result = provider.turn(model, messages, system, tool_specs,
                                           cb.on_text, cb.cancelled)
                    turn.last_request_latency = time.monotonic() - _t0
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
        turn.reasoning_tokens += getattr(result, "reasoning_tokens", 0) or 0
        turn.reasoning_chars += getattr(result, "reasoning_chars", 0) or 0
        if cb.on_usage is not None:
            # R192: the cached count rides along so the engine can price the
            # round; it is a SUBSET of input_tokens, never additive to it.
            cb.on_usage(result.input_tokens, result.output_tokens,
                        getattr(result, "cached_input_tokens", 0) or 0)

        if result.stop_reason == "cancelled":
            cb.notify("interrupted")
            turn.cancelled = True
            return turn

        # R58 gap fix (2026-07-27): the assistant's OWN generated text was
        # never scanned — only the user's prompt and tool output were. A
        # model that echoes a secret back in its prose (copying a token from
        # an earlier tool result into its answer) streamed it straight to
        # the screen via cb.on_text AND stored it in `messages` unredacted,
        # where it would be re-sent to the provider on every later round.
        # This can't un-stream what already reached the screen (on_text
        # already ran chunk-by-chunk inside provider.turn(), above) — but it
        # keeps a "redact" choice from persisting the secret in history/the
        # session log, and "stop" from letting a mid-turn reply's secret
        # trigger any tool calls that came with it.
        if cb.secret_challenge and result.text:
            reply_matches = secretscan.scan(result.text, cb.secret_allowlist)
            if reply_matches:
                decision = cb.secret_challenge("reply", reply_matches,
                                               source_text=result.text)
                if decision == "stop":
                    # P-1 fix: returning here without appending anything left
                    # `messages` ending on the USER turn `Engine.send` already
                    # appended — the next `send()` then appends ANOTHER user
                    # message on top of it (two consecutive user turns, the
                    # R44/R128 invalid sequence), and any tool_calls this
                    # round carried were silently dropped with no _skip()/
                    # _flush(). Redact and record a (tool-call-free) assistant
                    # message instead, so history stays a valid, closed turn
                    # and the raw secret never lands in it either way.
                    cb.notify("stopped: secret detected in the assistant's reply")
                    result.text = secretscan.redact(result.text, reply_matches)
                    result.tool_calls = []
                    messages.append(provider.assistant_message(result))
                    turn.produced = True
                    turn.cancelled = True
                    return turn
                elif decision == "redact":
                    result.text = secretscan.redact(result.text, reply_matches)

        # R58 gap fix (2026-07-27): scan write_file/edit_file/apply_patch's
        # actual written content BEFORE the assistant message (which carries
        # these arguments verbatim) enters history — a redact here mutates
        # `call.arguments` in place, so both the historical record AND the
        # write that runs later this round see the same redacted text.
        # Must happen before `messages.append` below, not in the per-call
        # loop further down: by the time that loop runs, `result` is already
        # the one being turned into the stored assistant message.
        if cb.secret_challenge:
            for call in result.tool_calls:
                field = _WRITE_ARG_FIELD.get(call.name)
                if not field:
                    continue
                text = str(call.arguments.get(field, ""))
                if not text:
                    continue
                write_matches = secretscan.scan(text, cb.secret_allowlist)
                if not write_matches:
                    continue
                decision = cb.secret_challenge(f"write:{call.name}", write_matches,
                                               source_text=text)
                if decision == "stop":
                    # R265: close the turn the way the reply-scan stop above
                    # does. Returning bare left `messages` ending on the
                    # user turn (next send stacks two user messages) and
                    # the requested calls never reached on_tool_result.
                    cb.notify(f"stopped: secret detected in a {call.name} argument")
                    for c in result.tool_calls:
                        cb.on_tool_result(
                            c.name, "[skipped: secret detected — user stopped the turn]")
                    result.tool_calls = []
                    if not result.text:
                        result.text = "[stopped: secret detected in a tool argument]"
                    messages.append(provider.assistant_message(result))
                    turn.produced = True
                    turn.cancelled = True
                    return turn
                elif decision == "redact":
                    call.arguments[field] = secretscan.redact(text, write_matches)

        messages.append(provider.assistant_message(result))
        turn.produced = True

        if not result.tool_calls:
            return turn  # final answer

        # A round's results are flushed as ONE unit through a provider's
        # optional tool_results_messages() hook (bulk API), if it has one —
        # otherwise one message per result via tool_result_message().
        round_out: list[tuple] = []  # (ToolCall, output)

        def _skip(calls, reason: str) -> None:
            """Answer `calls` without running them.

            R134g: these used to extend `round_out` directly, which keeps
            history valid but bypasses `cb.on_tool_result` — so a call
            abandoned at the approval gate, at the iteration cap, or by
            Ctrl+C produced NO session record at all. The log then said a
            turn made two tool calls when the model asked for five, and
            `/context`'s tools: count undercounted every stopped turn. Routing
            them through the callback logs them and shows them, which is
            also the honest thing for the user: the model asked, and this is
            what happened to the request."""
            for c in calls:
                cb.on_tool_result(c.name, reason)
                round_out.append((c, reason))

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
                _skip(result.tool_calls,
                      "[skipped: user stopped at the iteration cap]")
                _flush()
                return turn
            if go_on == "silent":
                # user chose to keep going and not be asked again this turn
                silent_continue = True
                cb.notify("continuing — won't ask again this turn")
            else:
                checkpoint = iteration + max_iterations

        this_round_results: dict[tuple, str] = {}

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
                     if c.name in tools.PARALLEL_SAFE
                     # R283: a gated fetch must wait for its approval
                     and not tools.needs_approval(c.name, c.arguments)
                     # R301: nor may a denied one run in the prefetch
                     and not (c.name in approve._URL_TOOLS
                              and (deny_broken or approve.is_denied(c.name, c.arguments, deny)))]
            if len(batch) > 1:
                for i, name, args in batch:
                    cb.on_tool_start(name, args)   # announce before running
                prefetched = tools.run_tools_parallel(batch, cancel=cb.cancelled)

        for idx, call in enumerate(result.tool_calls):
            def _finish(out: str) -> None:
                # user guidance rides on the round's last tool result so the
                # model reads it before its next step
                if guidance and idx == len(result.tool_calls) - 1:
                    out += f"\n[user guidance: {guidance}]"
                cb.on_tool_result(call.name, out)
                round_out.append((call, out))

            def _gate(decision: str, detail: str = "") -> None:
                # R133c: never let a logging callback kill the turn (same
                # contract as R42/R51 — the work matters, the record doesn't)
                if cb.on_approval is None:
                    return
                try:
                    cb.on_approval(call.name, decision, detail)
                except Exception:
                    pass

            if cb.cancelled():
                cb.notify("interrupted")
                # keep history valid: answer the remaining calls as skipped
                _skip(result.tool_calls[idx:], "[skipped: interrupted]")
                _flush()
                turn.cancelled = True
                return turn
            # R301: URL tools consult the denylist even when ungated — an
            # "always deny" origin rule must also stop that origin's plain
            # public pages ("deny always wins", R120).
            if ((tools.needs_approval(call.name, call.arguments)
                 or call.name in approve._URL_TOOLS)
                    and (deny_broken or approve.is_denied(call.name, call.arguments, deny))):
                # R120: a denylist match skips the prompt entirely — no
                # question asked, same as pi-permission-system's fail-closed
                # "deny always wins" design. Checked before is_allowed: a
                # call matching both an allow and a deny rule is a config
                # mistake, not a case worth new precedence rules for.
                cb.notify(f"denied by policy: {call.name}")
                _gate("denied_policy")
                _finish("[denied by policy]")
                turn.events.append({"tool": call.name, "denied": True,
                                    "policy": True})
                continue
            if (tools.needs_approval(call.name, call.arguments) and not approve.is_allowed(
                    call.name, call.arguments, allow)
                    and not (cb.auto_approve and cb.auto_approve())):
                diff = approve.diff_preview(call.name, call.arguments)
                # R103: "explain" re-asks the SAME challenge after showing a
                # model-written description of what the call will do — never
                # a terminal answer on its own, so it loops rather than
                # falling through to the y/a/n/s/c handling below.
                while True:
                    ans, note = _norm(cb.approve(call.name, call.arguments, diff))
                    if ans != "e":
                        break
                    cb.notify(_explain_tool_call(provider, model, call.name,
                                                 call.arguments,
                                                 cancel=cb.cancelled))
                if ans == "a":
                    rule = approve.add_rule(call.name, call.arguments)
                    cb.notify(f"always-allow added: {call.name} · {rule}")
                    allow = _load_allow_or_empty(cb)   # R196: same guard
                    _gate("always_allow", rule)
                elif ans == "d":
                    rule = approve.add_deny_rule(call.name, call.arguments)
                    cb.notify(f"always-deny added: {call.name} · {rule}")
                    try:
                        deny, deny_broken = approve.load_deny(), False
                    except approve.ApproveLoadError as e:
                        cb.notify(f"denylist.yaml is unreadable ({e}) — "
                                  "blocking all gated tool calls until it's fixed")
                        deny, deny_broken = {}, True
                    _gate("always_deny", rule)
                    _finish("[denied by policy]")
                    turn.events.append({"tool": call.name, "denied": True,
                                        "policy": True})
                    continue
                elif ans == "s":
                    cb.notify("stopped by user")
                    _gate("stopped")
                    _skip(result.tool_calls[idx:],
                          "[skipped: user stopped the turn]")
                    _flush()
                    return turn
                elif ans == "c":
                    _gate("steered", note)
                    _finish(f"[not run — user guidance: {note}]")
                    turn.events.append({"tool": call.name, "steered": note})
                    continue
                elif ans != "y":
                    _gate("denied", note)
                    _finish(f"[denied by user: {note}]" if note
                            else "[denied by user]")
                    turn.events.append({"tool": call.name, "denied": True})
                    continue
                else:
                    _gate("approved", note)
            elif tools.needs_approval(call.name, call.arguments) and not approve.is_allowed(
                    call.name, call.arguments, allow):
                # R262: `/auto-approve on` — skip the prompt exactly like an
                # allowlist hit, but logged under its own decision so the
                # session log still shows WHY nobody was asked. Denylist
                # still wins (checked, and `continue`d past, above this
                # whole if/elif chain) — this only ever bypasses the ASK,
                # never a standing refusal.
                _gate("auto_approved")
            elif tools.needs_approval(call.name, call.arguments):
                # the gate was passed without a question — an existing
                # allowlist rule matched. The common path for a daily driver,
                # and invisible in the log until R133c.
                _gate("allowlisted")
            # R58: run_command's PARAMETERS are the deliberate exception to
            # the keep/redact/stop challenge — the command needs its real
            # argument to actually work (a real key in a curl header, say),
            # so silently altering it would just break it, and blocking would
            # duplicate the approval gate it already went through above. This
            # is a NOTICE only: it never blocks, never touches what runs.
            # R131: `wait_until` (R100) is the second shell entry point and
            # takes the same `command` argument — it inherited run_command's
            # allowlist shape (approve._COMMAND_TOOLS) and its process-group
            # hardening (R95c), but not this notice, so a credential in a
            # polled command went unflagged where the identical string in a
            # one-shot command was reported. An omission, not a decision.
            # R170g: `secretscan.scan` only ever sees the literal `command`
            # STRING — it can't decode/execute anything, so a secret piped
            # through `base64 -d`, hex, or built up via `$(...)` substitution
            # never appears in the text it scans and is invisible to this
            # notice. That's an inherent limit of a static text scan, not a
            # bug to fix here — but the wording said "possible secret" with
            # nothing warning that a clean scan doesn't mean a clean command,
            # which reads as more assurance than the check can back up.
            if call.name in _COMMAND_ARG_TOOLS and cb.secret_challenge:
                # wait_until's optional `then` (feature request, 2026-07-27)
                # is a second shell command argument, same shape as
                # `command` — scanned together so a secret placed in the
                # follow-up isn't invisible to this notice just because it
                # landed in the newer field.
                scanned = call.arguments.get("command", "")
                if call.arguments.get("then"):
                    scanned += "\n" + call.arguments["then"]
                param_matches = secretscan.scan(scanned, cb.secret_allowlist)
                if param_matches:
                    cb.notify(f"possible secret in this command "
                             f"(raw-text scan, easy to evade via encoding — "
                             f"not a guarantee): {secretscan.preview(param_matches)}")
            # R47: snapshot the tree before any mutation lands (approved or
            # allowlisted) — /rewind restores to this point. R181: the
            # callback also gets `call.arguments` now, so it can ALSO
            # snapshot the single target FILE when the call names one
            # unambiguously (write_file/edit_file/apply_patch all take
            # `path`) — regardless of whether that path is inside the
            # checkpointed tree at all. The whole-tree checkpoint alone is
            # blind to anything outside cwd (`rewind.covers()`'s documented
            # gap); the per-file snapshot is what lets `/undo` work on "the
            # file I just told you to edit" when that file lives somewhere
            # else entirely (a Desktop file, say) — the case that broke
            # twice in production.
            if tools.needs_approval(call.name) and cb.checkpoint is not None:
                cb.checkpoint(call.name, call.arguments)
            if idx in prefetched:
                out = prefetched[idx]   # already ran (and announced) in the
                # R94 parallel batch above
            else:
                cb.on_tool_start(call.name, call.arguments)
                out = tools.run_tool(call.name, call.arguments,
                                     cancel=cb.cancelled)   # R272
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
                        # R143c: the one abandon site R134g missed — it
                        # extended round_out directly, so these calls never
                        # reached cb.on_tool_result and produced no session
                        # record at all, undercounting /context's tools: exactly
                        # as the other three did before R134g.
                        _skip(result.tool_calls[idx:],
                              "[skipped: secret detected — user stopped the turn]")
                        _flush()
                        return turn
                    elif decision == "redact":
                        out = secretscan.redact(out, matches)
            # loop nudge: the model just repeated last round's exact call
            # AND got the exact same result — tell it so instead of silently
            # feeding the same output again. Keyed on the result too, not
            # just the call, so a deliberate re-run (write -> test -> fix ->
            # re-test) whose output changed is never told it's a no-op repeat.
            key = (call.name, repr(sorted(call.arguments.items())))
            if last_results.get(key) == out:
                out += ("\n[note: you already ran this exact call with this "
                        "exact result — do not repeat it; use the result "
                        "above or give your final answer]")
            this_round_results[key] = out
            _finish(out)
            turn.events.append({"tool": call.name, "args": call.arguments})
        last_results = this_round_results
        _flush()
