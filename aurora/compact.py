"""History flattening — shared by cross-provider model switches (R4) and
/compact (R14). Turns an OpenAI-compat message list (tool_calls/tool role)
into a single plain-text transcript, which any provider can then consume as
one user message. Tool calls don't translate 1:1 to a resumed turn's
format, so we flatten rather than re-encode."""

from . import tokens


def _stringify(content) -> str:
    """R222: `content` is None for a tool-calls-only assistant message (the
    ordinary OpenAI-format shape — text and tool_calls are independent
    fields, and a pure tool call carries no text). `str(None)` used to turn
    that into the literal 4 characters "None" — `flatten_history` then
    printed a visible "Assistant: None" line for a turn where the assistant
    said nothing at all, and `_msg_tokens` overcounted every such message by
    a few tokens it never actually spent."""
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


def _msg_tokens(m: dict) -> int:
    n = tokens.estimate_tokens(_stringify(m.get("content")))
    for tc in (m.get("tool_calls") or []):
        fn = tc.get("function", {})
        n += tokens.estimate_tokens(str(fn.get("arguments", "")))
    return n


def is_cut_boundary(m: dict) -> bool:
    """R156: may `messages[i:]` start here and still be a valid request?

    Two roles can open a sequence. A `user` message always could (R118). So
    can an `assistant` message carrying `tool_calls`: it OPENS a round, so
    the `tool` results answering it come after, and no `tool` message is
    left referring to a `tool_calls` entry that got folded away. A bare
    `assistant` (no tool_calls) or a `tool` message cannot — those sit in
    the MIDDLE of a round."""
    role = m.get("role")
    return role == "user" or (role == "assistant" and bool(m.get("tool_calls")))


def cut_index(messages: list[dict], keep_recent_tokens: int) -> int:
    """R118: the index where the "keep raw" tail begins — everything before
    it is old enough to fold into a summary. Walks backward accumulating
    each message's estimated size until `keep_recent_tokens` is exceeded,
    then forward to the nearest `is_cut_boundary` message, so the cut never
    separates a `tool` message from the `tool_calls` entry it answers.

    R156: that forward walk used to accept ONLY a `user` message, which made
    auto-compact structurally unable to rescue the case it exists for. Inside
    a single turn there IS no later user message — a turn is `user ->
    assistant(+tool calls) -> tool(s) -> ...` — so a turn that read a few big
    files walked off the end and returned 0, i.e. "nothing foldable", while
    holding 90k tokens against a 65k window. The engine then sent the
    oversized request and the provider rejected the whole turn. Round
    boundaries inside the turn are legal cut points too; accepting them is
    what lets a runaway turn shrink itself.

    Returns 0 ("leave history alone") when the whole conversation fits inside
    `keep_recent_tokens`, or when no safe boundary exists at all."""
    total = 0
    idx = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        total += _msg_tokens(messages[i])
        if total > keep_recent_tokens:
            idx = i
            break
    else:
        return 0   # the whole history fits inside the recent-keep budget
    stop = idx
    while idx < len(messages) and not is_cut_boundary(messages[idx]):
        idx += 1
    if idx < len(messages):
        return idx
    # R158: no boundary at or after the stop point. R156 gave up here and
    # returned 0 — "nothing foldable" — which is wrong whenever a boundary
    # exists EARLIER, and that is the common case, not a corner one: a single
    # `read_file` of a large source file produces one tool result bigger than
    # `keep_recent_tokens` all by itself, so the backward walk stops ON that
    # result with nothing but the end of the list ahead of it. Fall back to
    # the LAST boundary before the stop point: it keeps a bigger raw tail than
    # the budget asked for (that one huge message is unsplittable, so no cut
    # can do better), but it folds everything in front of it instead of
    # folding nothing at all.
    for i in range(stop - 1, -1, -1):
        if is_cut_boundary(messages[i]):
            return i
    return 0   # genuinely no safe cut anywhere


def flatten_history(messages: list[dict]) -> str:
    """Full transcript as readable text."""
    lines = []
    for m in messages:
        role = m.get("role", "?")
        if role == "tool":  # openai tool result
            # R222 applies here too: a null `content` must render as empty,
            # not as the literal "None". This branch kept using the raw
            # `.get` when the rest of the function moved to `_stringify`.
            lines.append(f"[tool result: {_stringify(m.get('content'))}]")
            continue
        text = _stringify(m.get("content"))
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                text += f"\n[called {fn.get('name')}({fn.get('arguments')})]"
        prefix = {"user": "User", "assistant": "Assistant"}.get(role, role)
        if text.strip():
            lines.append(f"{prefix}: {text}")
    return "\n\n".join(lines)


def _clip_marker(dropped: int) -> str:
    """The elision notice `clip_transcript` splices between head and tail.
    Named so its LENGTH can be reserved out of the budget (R229)."""
    return (f"\n\n[... {dropped} characters dropped to fit the context "
            "window — middle of the folded region ...]\n\n")


def clip_transcript(text: str, max_tokens: int) -> str:
    """R156: bound a transcript to `max_tokens`, keeping the head and the tail
    and dropping the middle.

    Two places needed this and neither had it. (1) The summarization request
    sends the transcript of everything being folded — for the very case
    auto-compact exists to fix, that transcript is ITSELF bigger than the
    window, so the summarization request got rejected with the same
    `exceed_context_size_error` as the request that triggered it. (2) The
    fallback when summarization fails was `flattened_as_user_message`, a
    VERBATIM copy of the whole region — so the "fold" carried exactly as many
    tokens as it removed and the turn died anyway.

    Head and tail rather than a plain head: the start carries what the task
    is, the end carries where it got to, and the middle of a runaway turn is
    the bulk file dumps that are least worth keeping."""
    if max_tokens <= 0:
        return text
    limit = max_tokens * tokens.CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    # R229: the marker counts against the budget. It used to be added ON TOP
    # of `limit`, so the result overran the bound this function exists to
    # enforce — by ~90 characters always, which at a small remaining budget
    # is most of it (max_tokens=10 returned ~32 tokens' worth). Both callers
    # pass a budget computed from what is left of the context window, so an
    # overrun here is the very rejection being avoided.
    #
    # Sizing the reservation off `len(text)` (an upper bound on `dropped`,
    # hence on the marker's digit count) keeps this a single pass: the real
    # marker can only be shorter, never longer, so the total stays inside
    # `limit`.
    reserve = len(_clip_marker(len(text)))
    budget = limit - reserve
    if budget < 2:
        # The budget cannot hold head, tail and an explanation. Honour the
        # bound rather than the shape — a caller this starved needs the
        # request to fit far more than it needs the marker.
        return text[:limit]
    head = int(budget * 0.6)
    tail = budget - head
    dropped = len(text) - head - tail
    return text[:head] + _clip_marker(dropped) + text[-tail:]


def flattened_as_user_message(messages: list[dict]) -> dict:
    """Wrap a flattened transcript as a single user message."""
    transcript = flatten_history(messages)
    body = ("[Earlier conversation, carried over on model switch:]\n\n"
            + transcript)
    return {"role": "user", "content": body}
