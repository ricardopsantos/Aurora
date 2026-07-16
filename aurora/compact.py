"""History flattening — shared by cross-provider model switches (R4) and
/compact (R14). Turns an OpenAI-compat message list (tool_calls/tool role)
into a single plain-text transcript, which any provider can then consume as
one user message. Tool calls don't translate 1:1 to a resumed turn's
format, so we flatten rather than re-encode."""


def _stringify(content) -> str:
    return content if isinstance(content, str) else str(content)


def flatten_history(messages: list[dict]) -> str:
    """Full transcript as readable text."""
    lines = []
    for m in messages:
        role = m.get("role", "?")
        if role == "tool":  # openai tool result
            lines.append(f"[tool result: {m.get('content', '')}]")
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


def flattened_as_user_message(messages: list[dict]) -> dict:
    """Wrap a flattened transcript as a single user message."""
    transcript = flatten_history(messages)
    body = ("[Earlier conversation, carried over on model switch:]\n\n"
            + transcript)
    return {"role": "user", "content": body}
