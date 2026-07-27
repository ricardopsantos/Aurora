"""Lightweight streaming markdown → ANSI for the terminal (display only —
history, /copy and /export always keep the raw markdown). Line-based so it
works mid-stream: the UI buffers until each newline and renders whole lines.

Deliberately small: bold, inline code, headers, bullets, dim code fences —
with keyword/string/number highlighting inside a fence when its language tag
(the text after ```` ``` ````) is one R164 knows. When colours are off
(NO_COLOR / non-tty) the raw text passes through untouched, so pipes see
exactly what the model wrote."""

import re

from .colors import DIM, BOLD, CYAN, GREEN, MAGENTA, RESET, YELLOW, linkify

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_BULLET = re.compile(r"^(\s*)[*-]\s+")
_HEADER = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_LANG = re.compile(r"^```\s*([A-Za-z0-9+_-]*)")

# R164: a curated set, not every language — each entry costs one compiled
# regex kept for the process lifetime, and a language absent here just falls
# back to the plain dim-fence rendering that was already correct.
_KEYWORDS = {
    "python": {"def", "class", "import", "from", "return", "if", "elif",
              "else", "for", "while", "in", "not", "and", "or", "is",
              "None", "True", "False", "try", "except", "finally", "with",
              "as", "lambda", "yield", "pass", "break", "continue", "raise",
              "global", "nonlocal", "assert", "async", "await", "del",
              "self", "print"},
    "javascript": {"function", "return", "if", "else", "for", "while", "do",
                   "var", "let", "const", "class", "extends", "new", "this",
                   "typeof", "instanceof", "in", "of", "try", "catch",
                   "finally", "throw", "switch", "case", "default", "break",
                   "continue", "async", "await", "import", "export", "from",
                   "null", "undefined", "true", "false", "interface",
                   "type", "implements", "public", "private"},
    "bash": {"if", "then", "else", "elif", "fi", "for", "while", "do",
             "done", "case", "esac", "function", "local", "export",
             "return", "echo", "in", "break", "continue", "exit"},
    "go": {"func", "package", "import", "return", "if", "else", "for",
           "range", "switch", "case", "default", "break", "continue", "var",
           "const", "type", "struct", "interface", "map", "chan", "go",
           "defer", "select", "nil", "true", "false"},
    "rust": {"fn", "let", "mut", "return", "if", "else", "for", "while",
             "loop", "match", "struct", "enum", "impl", "trait", "pub",
             "use", "mod", "self", "Self", "true", "false", "None", "Some",
             "Ok", "Err", "async", "await", "move", "ref", "as", "in"},
    "ruby": {"def", "end", "class", "module", "return", "if", "elsif",
             "else", "unless", "for", "while", "until", "case", "when",
             "do", "yield", "self", "nil", "true", "false", "begin",
             "rescue", "ensure", "raise", "require", "attr_accessor"},
}
_LANG_ALIASES = {
    "py": "python", "js": "javascript", "jsx": "javascript",
    "ts": "javascript", "tsx": "javascript", "sh": "bash", "shell": "bash",
    "zsh": "bash", "rb": "ruby", "rs": "rust",
}
_LINE_COMMENT = {"python": "#", "bash": "#", "ruby": "#", "javascript": "//",
                 "go": "//", "rust": "//"}


def _resolve_lang(tag: str) -> str | None:
    tag = tag.strip().lower()
    tag = _LANG_ALIASES.get(tag, tag)
    return tag if tag in _KEYWORDS else None


def _build_token_re(lang: str) -> re.Pattern:
    comment = _LINE_COMMENT.get(lang, "#")
    kw_alt = "|".join(sorted((re.escape(k) for k in _KEYWORDS[lang]),
                             key=len, reverse=True))
    return re.compile(
        rf'(?P<comment>{re.escape(comment)}.*$)|'
        r'(?P<string>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')|'
        r'(?P<number>\b\d+\.?\d*\b)|'
        rf'(?P<keyword>\b(?:{kw_alt})\b)')


_TOKEN_RES = {lang: _build_token_re(lang) for lang in _KEYWORDS}


def _highlight_code_line(line: str, lang: str | None) -> str:
    token_re = _TOKEN_RES.get(lang) if lang else None
    if token_re is None:
        return f"{DIM}{line}{RESET}"
    out = []
    last = 0
    colour = {"comment": DIM, "string": GREEN, "number": YELLOW,
             "keyword": MAGENTA}
    for m in token_re.finditer(line):
        out.append(f"{DIM}{line[last:m.start()]}{RESET}")
        out.append(f"{colour[m.lastgroup]}{m.group()}{RESET}")
        last = m.end()
    out.append(f"{DIM}{line[last:]}{RESET}")
    return "".join(out)


class LineRenderer:
    """Stateful per-turn renderer (tracks ``` fences + the fence's language)."""

    def __init__(self):
        self.in_fence = False
        self.fence_lang: str | None = None

    def render(self, line: str) -> str:
        if not RESET:  # colours disabled — stay byte-faithful
            return line
        if line.strip().startswith("```"):
            self.in_fence = not self.in_fence
            if self.in_fence:
                m = _FENCE_LANG.match(line.strip())
                self.fence_lang = _resolve_lang(m.group(1)) if m else None
            else:
                self.fence_lang = None
            return f"{DIM}{line}{RESET}"
        if self.in_fence:
            return _highlight_code_line(line, self.fence_lang)
        m = _HEADER.match(line)
        if m:
            return f"{BOLD}{CYAN}{m.group(2)}{RESET}"
        line = _BOLD.sub(f"{BOLD}\\1{RESET}", line)
        line = _CODE.sub(f"{CYAN}\\1{RESET}", line)
        line = _BULLET.sub(r"\1• ", line)
        return linkify(line)
