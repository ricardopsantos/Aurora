"""History flattening — shared by cross-provider model switches (R4) and
/compact (R14). Turns provider-native message lists (Anthropic content blocks
OR OpenAI tool_calls/tool role) into a single plain-text transcript, which any
provider can then consume as one user message. Tool blocks don't translate 1:1
between APIs, so we flatten rather than re-encode."""


def _stringify(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # anthropic content blocks
        parts = []
        for b in content:
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "tool_use":
                parts.append(f"[called {b.get('name')}({b.get('input')})]")
            elif t == "tool_result":
                parts.append(f"[tool result: {b.get('content')}]")
        return "\n".join(parts)
    return str(content)


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


def flattened_as_user_message(messages: list[dict], provider_kind: str) -> dict:
    """Wrap a flattened transcript as a single user message for the target
    provider (both kinds accept plain string content)."""
    transcript = flatten_history(messages)
    body = ("[Earlier conversation, carried over on model switch:]\n\n"
            + transcript)
    if provider_kind == "anthropic":
        return {"role": "user", "content": [{"type": "text", "text": body}]}
    return {"role": "user", "content": body}
