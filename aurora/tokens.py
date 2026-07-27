"""Local token heuristics — display/estimation only, no tokenizer, no network.

Engine-side on purpose (R90a): the engine needs `estimate_tokens` after a
/compact fold, and reaching into `ui.py` for it pulled prompt_toolkit into
the engine half, breaking the R25 boundary. Both halves import it from here.
"""


# The one heuristic this module is built on — the common English-text rule of
# thumb. Named because R156's `compact.clip_transcript` has to convert a token
# budget back into a character budget, and an unnamed `* 4` there would be the
# inverse of an unnamed `// 4` here, free to drift apart silently.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough, LOCAL token estimate (~4 chars/token, the common English-text
    rule of thumb) — no tokenizer dependency, no network call, good enough
    for a live "this draft will cost about N tokens" hint while typing. NOT
    the real count (that only exists after the provider's actual response);
    never used for anything but display."""
    return len(text) // CHARS_PER_TOKEN


def estimate_tokens_from_chars(chars: int) -> int:
    """Same heuristic as `estimate_tokens`, for a caller that already has a
    LENGTH and not the string (R154 counts a tool result's size without
    holding its 60KB body a second time).

    A separate function rather than an overload on purpose: passing a char
    count to `estimate_tokens` is a real bug that already shipped once —
    `len(int)` raises `TypeError: object of type 'int' has no len()`, which
    is how R74e's status-bar crash happened. Two names, no ambiguity."""
    return chars // CHARS_PER_TOKEN


def fmt_token_count(n: int) -> str:
    """Compact token count: 950 → '950', 1000 → '1k', 1500 → '1.5k'."""
    if n >= 1000:
        s = f"{n / 1000:.1f}"
        s = s.removesuffix(".0")
        return s + "k"
    return str(n)
