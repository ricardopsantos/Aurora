"""Anthropic Messages API — streaming, tool use, prompt caching (R15)."""

import json
from typing import Callable

import httpx

from .base import (Provider, ProviderError, ToolCall, TurnResult,
                   cancellable_sse)

# context windows + USD per MTok (input, output) — static table, R13
MODELS = {
    "claude-sonnet-5":            (200_000, 3.00, 15.00),
    "claude-opus-4-8":            (200_000, 15.00, 75.00),
    "claude-haiku-4-5-20251001":  (200_000, 1.00, 5.00),
}


def _to_anthropic_tools(specs: list[dict]) -> list[dict]:
    return [{"name": s["name"], "description": s["description"],
             "input_schema": s["parameters"]} for s in specs]


class AnthropicProvider(Provider):
    def context_limit(self, model: str) -> int:
        return MODELS.get(model, (200_000,))[0]

    def cost(self, model: str, inp: int, out: int) -> float:
        _, ci, co = MODELS.get(model, (0, 0.0, 0.0))
        return (inp * ci + out * co) / 1_000_000

    def turn(self, model, messages, system, tools, on_text, cancel) -> TurnResult:
        headers = {"Content-Type": "application/json",
                   "x-api-key": self.api_key,
                   "anthropic-version": "2023-06-01"}
        # cache_control on the (large, bootstrap-heavy) system prompt — R15
        if isinstance(system, str):
            system = [{"type": "text", "text": system,
                       "cache_control": {"type": "ephemeral"}}]
        payload = {"model": model, "max_tokens": 8192, "messages": messages,
                   "system": system, "stream": True}
        if tools:
            payload["tools"] = _to_anthropic_tools(tools)

        result = TurnResult()
        cur_tool = None          # (id, name, json-fragments)
        base = self.base_url or "https://api.anthropic.com"
        try:
                for kind, a, _b in cancellable_sse(
                        lambda: httpx.stream(
                            "POST", f"{base}/v1/messages", headers=headers,
                            json=payload,
                            timeout=httpx.Timeout(self.timeout, connect=5)),
                        cancel):
                    if kind == "status":
                        if a >= 400:
                            raise ProviderError(f"Anthropic HTTP {a}: {_b or ''}")
                        continue
                    line = a
                    if not line.startswith("data:"):
                        continue
                    ev = json.loads(line[5:].strip())
                    t = ev.get("type")
                    if t == "message_start":
                        result.input_tokens = ev["message"]["usage"].get("input_tokens", 0)
                        # cached tokens still occupy context; count them in
                        u = ev["message"]["usage"]
                        result.input_tokens += u.get("cache_read_input_tokens", 0)
                        result.input_tokens += u.get("cache_creation_input_tokens", 0)
                    elif t == "content_block_start":
                        block = ev.get("content_block", {})
                        if block.get("type") == "tool_use":
                            cur_tool = [block["id"], block["name"], []]
                    elif t == "content_block_delta":
                        d = ev.get("delta", {})
                        if d.get("type") == "thinking_delta" and self.on_think:
                            self.on_think(d.get("thinking", ""))
                        if d.get("type") == "text_delta":
                            result.text += d["text"]
                            on_text(d["text"])
                        elif d.get("type") == "input_json_delta" and cur_tool:
                            cur_tool[2].append(d.get("partial_json", ""))
                    elif t == "content_block_stop" and cur_tool:
                        raw = "".join(cur_tool[2]) or "{}"
                        try:
                            args = json.loads(raw)
                        except json.JSONDecodeError:
                            args = {"_malformed": raw}
                        result.tool_calls.append(ToolCall(cur_tool[0], cur_tool[1], args))
                        cur_tool = None
                    elif t == "message_delta":
                        result.output_tokens = ev.get("usage", {}).get("output_tokens", 0)
                        result.stop_reason = ev.get("delta", {}).get("stop_reason") or result.stop_reason
        except httpx.HTTPError as e:
            raise ProviderError(f"Anthropic request failed: {e}") from e
        if cancel():  # watcher aborted the stream — never act on partials
            result.stop_reason = "cancelled"
            result.tool_calls.clear()
        return result

    def assistant_message(self, result: TurnResult) -> dict:
        content = []
        if result.text:
            content.append({"type": "text", "text": result.text})
        for c in result.tool_calls:
            content.append({"type": "tool_use", "id": c.id, "name": c.name,
                            "input": c.arguments})
        return {"role": "assistant", "content": content or [{"type": "text", "text": ""}]}

    def tool_result_message(self, call: ToolCall, output: str) -> dict:
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": output}]}

    def tool_results_messages(self, pairs: list) -> list[dict]:
        """ALL of a round's results in ONE user message — the API rejects
        consecutive user messages ("roles must alternate"), which separate
        per-result messages would produce on parallel tool calls."""
        if not pairs:
            return []
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": o}
            for c, o in pairs]}]
