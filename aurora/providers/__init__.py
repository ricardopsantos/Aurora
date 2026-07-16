from .base import Provider, ToolCall, TurnResult
from .openai_compat import OpenAICompatProvider


def make_provider(name: str, cfg: dict, timeout: float) -> Provider:
    return OpenAICompatProvider(name, cfg, timeout)
