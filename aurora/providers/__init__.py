from .base import Provider, ToolCall, TurnResult
from .anthropic import AnthropicProvider
from .openai_compat import OpenAICompatProvider


def make_provider(name: str, cfg: dict, timeout: float) -> Provider:
    ptype = cfg.get("type", "openai")
    if ptype == "anthropic":
        return AnthropicProvider(name, cfg, timeout)
    return OpenAICompatProvider(name, cfg, timeout)
