from .base import Provider, ToolCall, TurnResult
from .openai_compat import OpenAICompatProvider

# Re-exported on purpose: callers import the provider vocabulary from the
# package, not from .base directly.
__all__ = [
    "OpenAICompatProvider",
    "Provider",
    "ToolCall",
    "TurnResult",
    "make_provider",
]


def make_provider(name: str, cfg: dict, timeout: float) -> Provider:
    return OpenAICompatProvider(name, cfg, timeout)
