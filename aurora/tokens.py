"""Local token heuristics — display/estimation only, no tokenizer, no network.

Engine-side on purpose (R90a): the engine needs `estimate_tokens` after a
/compact fold, and reaching into `ui.py` for it pulled prompt_toolkit into
the engine half, breaking the R25 boundary. Both halves import it from here.
"""


def estimate_tokens(text: str) -> int:
    """Rough, LOCAL token estimate (~4 chars/token, the common English-text
    rule of thumb) — no tokenizer dependency, no network call, good enough
    for a live "this draft will cost about N tokens" hint while typing. NOT
    the real count (that only exists after the provider's actual response);
    never used for anything but display."""
    return len(text) // 4


def fmt_token_count(n: int) -> str:
    """Compact token count: 950 → '950', 1000 → '1k', 1500 → '1.5k'."""
    if n >= 1000:
        s = f"{n / 1000:.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return s + "k"
    return str(n)
