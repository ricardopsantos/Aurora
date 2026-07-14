"""Secret detection + redaction (R58). Pure text in, matches/text out — no
UI, no I/O, so it's trivially unit-testable and usable from both the engine
(user prompts) and the agent loop (tool output) without either owning policy.

Two passes: known key/token SHAPES (regex, deterministic — includes GUIDs/
UUIDs, sometimes used as API keys/session tokens, not just harmless
correlation IDs), plus an ENTROPY fallback for ad-hoc tokens that don't match
any known shape (a random 33-char string handed out by some internal tool has
no "shape" to match, but is still clearly not English text or a hex hash/git
SHA). The entropy pass runs only on spans the shape pass didn't already
claim, and explicitly excludes pure-hex strings (git SHAs, MD5/SHA digests)
as its own false-positive guard — narrower than "exclude anything hash- or
UUID-shaped": UUIDs are caught deliberately by the shape pass above."""

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass

# (name, compiled pattern). Every pattern's TOTAL match is what gets redacted —
# for the .env-style rule that means "KEY=value", not just the value, since
# hiding only the value while leaving `AWS_SECRET_ACCESS_KEY=` visible still
# tells a reader which secret it was (fine) but the assignment shape is kept
# for readability: see `redact()`.
PATTERNS: list[tuple[str, re.Pattern]] = [
    # GUIDs/UUIDs are sometimes used as API keys/session tokens (not just
    # harmless request/correlation IDs) — flagged as a real match, not left to
    # the entropy fallback (which explicitly excludes them as a hash/UUID
    # false-positive guard; this is the deliberate, narrower exception to that)
    ("GUID/UUID", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                             r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("AWS access key",     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token",       re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token",        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Stripe key",         re.compile(r"\b(?:sk|pk)_live_[A-Za-z0-9]{16,}\b")),
    ("OpenAI-style key",   re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("Bearer token",       re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*")),
    ("Private key block",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?"
                r"-----END [A-Z ]*PRIVATE KEY-----")),
    # .env-style assignment: KEY=value where KEY names a credential
    ("Env credential",
     re.compile(r"(?im)^[ \t]*[A-Za-z_][A-Za-z0-9_]*"
                r"(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD)"
                r"[A-Za-z0-9_]*[ \t]*=[ \t]*\S+")),
]


@dataclass(frozen=True)
class Match:
    kind: str
    start: int
    end: int
    text: str


def hash_value(value: str) -> str:
    """SHA-256 of a matched value — what the allowlist stores, never the raw
    value itself. A false positive gets saved to disk/config as this hash,
    not as plaintext, even though the value usually isn't a real secret."""
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


# ── entropy fallback (catches ad-hoc tokens with no known vendor prefix) ───
# A contiguous run of "token" characters, 20+ long — long enough that English
# words/identifiers rarely reach it unbroken, short enough to still catch a
# plain API key/token handed out by some internal tool. The character set is
# deliberately narrow: excluding '/' breaks up file paths, and excluding '.'
# keeps file extensions from gluing onto otherwise benign names.
_CANDIDATE_RE = re.compile(r"\b[A-Za-z0-9_\-+]{20,}\b")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
# Timestamps / dated filenames like 20260710_211200_* or 2026-07-10_* look
# random but are not secrets. Anchor at the start of the token so a random
# key that merely contains a year in the middle is still allowed.
_DATE_RE = re.compile(r"^\d{4}(?:[-_])?\d{2}(?:[-_])?\d{2}(?:_\d{6})?")
# bits/char; random mixed-case+digit text sits ~4.5-5.5, English prose/plain
# hex/identifiers sit lower — tuned against the false positives below
_ENTROPY_THRESHOLD = 3.6


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    counts = Counter(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_hash_or_uuid(token: str) -> bool:
    """A git SHA or MD5/SHA hex digest — common, harmless, high-entropy-
    looking strings that are NOT secrets. (A UUID also matches this shape,
    but is moot here: the GUID/UUID pattern above claims it before the
    entropy pass ever runs, per the deliberate policy that UUIDs ARE flagged
    — this guard's real job is excluding pure-hex hashes/SHAs.)"""
    return bool(_HEX_RE.match(token.replace("-", "")))


def _is_date_or_timestamp(token: str) -> bool:
    """Dated filenames/timestamps like 20260710_211200_... — high-entropy
    looking but not secret."""
    return bool(_DATE_RE.match(token))


def scan(text: str, allowlist: set[str] | None = None) -> list[Match]:
    """Every match across all patterns plus the entropy fallback, in document
    order. Overlapping matches keep only the first claim on that span (regex
    patterns run first) — never redact the same span twice, which would
    misalign indices.

    `allowlist`, if given, is a set of `hash_value()` hashes — a match whose
    exact text hashes to an allowlisted entry is dropped before returning,
    so a confirmed false positive never re-triggers the challenge."""
    if not text:
        return []
    found: list[Match] = []
    claimed: list[tuple[int, int]] = []

    def _overlaps(s: int, e: int) -> bool:
        return any(s < ce and cs < e for cs, ce in claimed)

    for name, pat in PATTERNS:
        for m in pat.finditer(text):
            s, e = m.span()
            if _overlaps(s, e):
                continue   # overlaps an earlier match — skip, don't double-count
            claimed.append((s, e))
            found.append(Match(name, s, e, m.group(0)))

    for m in _CANDIDATE_RE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        token = m.group(0)
        if _is_hash_or_uuid(token):
            continue
        if _is_date_or_timestamp(token):
            continue   # dated filenames / timestamps are not secrets
        has_digit = any(c.isdigit() for c in token)
        has_lower = any(c.islower() for c in token)
        has_upper = any(c.isupper() for c in token)
        if not (has_digit and has_lower and has_upper):
            continue   # real tokens are mixed-case+digit; kills long slugs/paths
        if _shannon_entropy(token) < _ENTROPY_THRESHOLD:
            continue
        claimed.append((s, e))
        found.append(Match("High-entropy token", s, e, token))

    found.sort(key=lambda mm: mm.start)
    if allowlist:
        found = [m for m in found if hash_value(m.text) not in allowlist]
    return found


def redact(text: str, matches: list[Match]) -> str:
    """Replace every matched span with <secret>, right-to-left so earlier
    spans' indices stay valid as later ones are substituted."""
    for m in sorted(matches, key=lambda mm: mm.start, reverse=True):
        text = text[:m.start] + "<secret>" + text[m.end:]
    return text


def preview(matches: list[Match], max_items: int = 5) -> str:
    """A short, human-readable summary for the challenge prompt — kinds
    found, not the raw secret text (the whole point is not to echo it back)."""
    kinds = [m.kind for m in matches]
    counts: dict[str, int] = {}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1
    parts = [f"{v}× {k}" if v > 1 else k for k, v in counts.items()]
    shown = parts[:max_items]
    more = len(parts) - len(shown)
    return ", ".join(shown) + (f" (+{more} more kind{'s' if more != 1 else ''})"
                               if more > 0 else "")


def _line_span(text: str, start: int, end: int, context: int = 40):
    """Return the (start, end) indices of the surrounding line fragment,
    limited to `context` chars on each side of the matched span."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    if le == -1:
        le = len(text)
    ls = max(ls, start - context)
    le = min(le, end + context)
    return ls, le


def format_matches(text: str, matches: list[Match],
                   context: int = 40, max_items: int = 10) -> list[str]:
    """Return human-readable lines for a secret challenge: one line per
    match showing the surrounding context with the matched token in bold
    (using ANSI escape codes so it stands out in both the classic REPL and
    the TUI's chat pane)."""
    BOLD = "\x1b[1m"
    RESET = "\x1b[0m"
    lines: list[str] = []
    for i, m in enumerate(matches[:max_items], 1):
        ls, le = _line_span(text, m.start, m.end, context)
        before = text[ls:m.start]
        token = text[m.start:m.end]
        after = text[m.end:le]
        lines.append(f"  {i}. {m.kind}: {before}{BOLD}{token}{RESET}{after}")
    if len(matches) > max_items:
        lines.append(f"  … and {len(matches) - max_items} more")
    return lines
