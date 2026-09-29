"""apply_patch (R97): a real multi-hunk diff tool. A model wanting to make
several small edits in one file today needs one edit_file call per anchor
(each needing its own unique `old` text) or a whole-file write_file (loses
granularity, riskier on a large file). apply_patch takes one unified diff —
the format every model has seen a million times as `git diff`/`diff -u`
output — and applies every hunk as ONE atomic change: all hunks match and
apply, or none do and nothing is written.

Hunk positions are found by CONTENT, never by the diff's own line numbers
(`@@ -l,s +l,s @@` headers are read but never trusted) — the same reason
edit_file requires a unique anchor: a model's line numbers drift the moment
any earlier hunk in the same patch has already changed the file, and a
patch generated from a slightly stale read is common. Each hunk's
context+removed lines must match EXACTLY ONCE in the file as it stands
after every earlier hunk in the same patch has already applied (hunks apply
IN ORDER, each against the previous hunk's result) — the same "old text
must match exactly and uniquely" contract edit_file already has, extended
to N hunks with an all-or-nothing outcome.

Pure engine-side module: no I/O, no UI imports, no file access — callers
(tools.py, approve.py) own reading/writing the file.
"""

import re
from dataclasses import dataclass


@dataclass
class Hunk:
    old: str      # context + removed lines, joined — the anchor to find
    new: str      # context + added lines, joined — what replaces it
    header: str   # the raw "@@ ... @@" line, for error messages only
    # R220: whether old_lines != new_lines at parse time — NOT the same as
    # `old == new` (the joined strings). A removal-only hunk consisting of a
    # single blank line has old_lines=[""] (len 1) and new_lines=[] (len 0):
    # different lists, but "\n".join(...) collapses BOTH to "" and the joined
    # strings collide. apply() used to skip on that string equality and
    # silently no-op the deletion of an isolated blank line.
    changed: bool = True


class PatchError(Exception):
    pass


_HUNK_HEADER = re.compile(r"^@@ .* @@")


def _is_file_header(lines: list[str], i: int) -> bool:
    """R236: does `lines[i]` begin a new FILE section, ending the hunk body?

    This used to be `lines[i].startswith(("---", "+++"))`, which cannot tell
    a file header from ordinary hunk CONTENT: removing a line whose text is
    `---` (a markdown horizontal rule, a YAML front-matter delimiter — both
    everywhere, this repo's own documents included) renders as `----`, and
    `"----".startswith("---")` is True. The hunk body was then cut off at
    that line and the REST of the hunk silently discarded, so the patch
    applied partially while reporting success — the exact opposite of the
    all-or-nothing contract this module exists to provide. Same trap for a
    removed `--`, and for an added `++` (`+++`).

    A real header is a PAIR: `--- <path>` immediately followed by
    `+++ <path>` (both `git diff` and `diff -u` emit them that way, marker
    then a space). Requiring the successor line makes a content collision
    take a removed `-- ...` line followed by an added `++ ...` line, which a
    diff cannot produce in that order within one hunk. `diff --git` /
    `diff -u` section lines are recognised directly."""
    line = lines[i]
    if line.startswith("diff "):
        return True
    if line.startswith("--- "):
        return i + 1 < len(lines) and lines[i + 1].startswith("+++ ")
    if line.startswith("+++ "):
        return i > 0 and lines[i - 1].startswith("--- ")
    return False


def parse(diff_text: str) -> list[Hunk]:
    """Parse a unified diff into hunks. `---`/`+++` file-header lines are
    read and discarded — the CALLER's own `path` argument is the only
    authority on which file gets written, never anything embedded in the
    diff text itself (a model-supplied header naming a different file must
    never redirect where the patch lands)."""
    lines = diff_text.splitlines()
    hunks: list[Hunk] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not _HUNK_HEADER.match(line):
            i += 1
            continue
        header = line
        i += 1
        old_lines: list[str] = []
        new_lines: list[str] = []
        while i < len(lines) and not _HUNK_HEADER.match(lines[i]) \
                and not _is_file_header(lines, i):
            raw = lines[i]
            if raw.startswith("\\"):        # "\ No newline at end of file"
                i += 1
                continue
            if raw == "":
                # a blank CONTEXT line — a model that forgot the leading
                # space marker for an empty source line is common; treat it
                # as context rather than rejecting the whole hunk
                old_lines.append("")
                new_lines.append("")
            elif raw[0] == " ":
                old_lines.append(raw[1:])
                new_lines.append(raw[1:])
            elif raw[0] == "-":
                old_lines.append(raw[1:])
            elif raw[0] == "+":
                new_lines.append(raw[1:])
            else:
                raise PatchError(
                    f"hunk {header!r}: bad line (must start with ' ', '-', "
                    f"'+', or be blank): {raw!r}")
            i += 1
        if not old_lines and not new_lines:
            raise PatchError(f"hunk {header!r}: empty — nothing to apply")
        if not old_lines:
            # every line was '+' — a pure insertion with no context/removed
            # line to anchor it. text.replace("", new, 1) would silently
            # insert at the very START of the file, almost never what's
            # intended, so this must be a clear error, not a silent
            # misapplication.
            raise PatchError(
                f"hunk {header!r}: pure insertion with no surrounding "
                f"context — include at least one unchanged line before or "
                f"after the insertion point")
        hunks.append(Hunk("\n".join(old_lines), "\n".join(new_lines), header,
                         changed=(old_lines != new_lines)))
    if not hunks:
        raise PatchError("no hunks found — expected a unified diff "
                         "(\"@@ ... @@\" hunk headers)")
    return hunks


def apply(text: str, hunks: list[Hunk]) -> str:
    """Apply every hunk IN ORDER, each against the result of the previous
    one — all-or-nothing: raises PatchError on the first hunk that doesn't
    match exactly once, and the caller must not have written anything to
    disk yet (this function never touches the filesystem)."""
    for h in hunks:
        if not h.changed:
            continue   # a hunk with no real change (every line was
            # context) — nothing to do, not an error
        hits = _line_anchored(text, h.old)
        n = len(hits)
        if n == 0:
            raise PatchError(
                f"hunk {h.header!r}: context not found — the file may have "
                f"changed since the patch was written, or a line is off by "
                f"one; re-read the file and regenerate the patch")
        if n > 1:
            raise PatchError(
                f"hunk {h.header!r}: context matches {n} times — add more "
                f"surrounding lines to make it unique")
        i = hits[0]
        text = text[:i] + h.new + text[i + len(h.old):]
    return text


def _line_anchored(text: str, old: str) -> list[int]:
    """R281: offsets where `old` matches as WHOLE lines — starting at the
    beginning of a line and ending at the end of one. A plain substring
    count let a hunk anchor inside an unrelated line: `-count = 5` edited
    `total_count = 5`, and reported success."""
    out, i = [], text.find(old)
    while i != -1:
        end = i + len(old)
        if (i == 0 or text[i - 1] == "\n") and \
                (end == len(text) or text[end] == "\n"):
            out.append(i)
        i = text.find(old, i + 1)
    return out
