# summary: R423 — edit config.yaml's `models:` list as TEXT (add / change / delete one entry) so every comment and every other line of the file survives; each result is re-parsed and refused unless it says exactly what was asked.
"""R423: comment-preserving edits of the `models:` list in config.yaml.

A YAML load + dump round-trip drops every comment in the file (that is how /model add and
/model remove used to work). Here the file is edited as text instead: only the lines of the
entry (or, for a change, of the one key) being edited are rewritten; everything else is kept
byte for byte. After each edit the new text is parsed and compared with what was asked for;
any difference refuses the write, so a layout this code does not understand can never be
mangled — it is reported instead ("edit config.yaml by hand")."""
import re

import yaml

TOP_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")


class EditError(Exception):
    pass


def _dump(obj, indent: int) -> list[str]:
    text = yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)
    return [" " * indent + ln + "\n" for ln in text.splitlines()]


def _is_blank_or_comment(ln: str) -> bool:
    s = ln.strip()
    return not s or s.startswith("#")


def _ind(ln: str) -> int:
    return len(ln) - len(ln.lstrip(" "))


class _Doc:
    """The file's lines plus where the models block and each of its entries sit."""

    def __init__(self, text: str):
        if "\t" in text:
            raise EditError("config.yaml uses tabs; edit it by hand")
        self.lines = text.splitlines(keepends=True)
        if self.lines and not self.lines[-1].endswith("\n"):
            self.lines[-1] += "\n"
        self.head = next((i for i, ln in enumerate(self.lines) if re.match(r"^models\s*:", ln)), None)
        self.items: list[tuple[int, int]] = []   # [start, end) of each entry, trailing comments/blanks excluded
        self.item_ind = 0
        if self.head is None:
            return
        rest = self.lines[self.head].split(":", 1)[1].split("#", 1)[0].strip()
        if rest not in ("", "[]"):
            raise EditError("models: is written inline; edit config.yaml by hand")
        self.inline_empty = rest == "[]"
        end = len(self.lines)
        for i in range(self.head + 1, len(self.lines)):
            ln = self.lines[i]
            if not _is_blank_or_comment(ln) and _ind(ln) == 0 and not ln.startswith("-"):
                end = i
                break
        starts = []
        for i in range(self.head + 1, end):
            ln = self.lines[i]
            if _is_blank_or_comment(ln):
                continue
            if ln.lstrip(" ").startswith("- ") or ln.strip() == "-":
                if not starts:
                    self.item_ind = _ind(ln)
                if _ind(ln) == self.item_ind:
                    starts.append(i)
            elif not starts:
                raise EditError("unexpected text under models:; edit config.yaml by hand")
        for k, s in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else end
            while e > s + 1 and _is_blank_or_comment(self.lines[e - 1]):
                e -= 1
            self.items.append((s, e))
        self.block_end = self.items[-1][1] if self.items else self.head + 1

    def text(self) -> str:
        return "".join(self.lines)

    # each top-level key of one entry: name -> [start, end) of its lines (the first key's line starts with "- ")
    def keys(self, n: int) -> dict:
        s, e = self.items[n]
        kind = self.item_ind + 2
        out, cur = {}, None
        for i in range(s, e):
            ln = self.lines[i]
            body = ln[kind:] if i > s else ln[self.item_ind + 2:]
            if i > s and (_is_blank_or_comment(ln) or _ind(ln) > kind):
                continue
            if i > s and _ind(ln) != kind:
                raise EditError("an entry under models: is laid out in a way Aurora cannot edit; edit config.yaml by hand")
            m = re.match(r"([A-Za-z_][\w-]*)\s*:", body)
            if not m:
                raise EditError("an entry under models: is not a plain mapping; edit config.yaml by hand")
            if cur:
                out[cur][1] = i
            cur = m.group(1)
            out[cur] = [i, e]
        for span in out.values():
            span[1] = self._trim(span[0], span[1])
        return out

    def _trim(self, s: int, e: int) -> int:
        while e > s + 1 and _is_blank_or_comment(self.lines[e - 1]):
            e -= 1
        return e


def _parse(text: str) -> dict:
    d = yaml.safe_load(text) or {}
    if not isinstance(d, dict):
        raise EditError("config.yaml is not a mapping")
    return d


def _check(old_text: str, new_text: str, want_models: list) -> str:
    old, new = _parse(old_text), _parse(new_text)
    if (new.get("models") or []) != want_models:
        raise EditError("the edit did not come out as asked; config.yaml is unchanged (edit it by hand)")
    if {k: v for k, v in old.items() if k != "models"} != {k: v for k, v in new.items() if k != "models"}:
        raise EditError("the edit would change more than models:; config.yaml is unchanged")
    return new_text


def entries(text: str) -> list:
    m = _parse(text).get("models")
    return list(m) if isinstance(m, list) else []


def add(text: str, entry: dict) -> str:
    want = entries(text) + [dict(entry)]
    doc = _Doc(text)
    if doc.head is None:
        tail = "" if not doc.lines or doc.lines[-1].endswith("\n") else "\n"
        new = doc.text() + tail + "models:\n" + "".join(_dump([entry], 0))
        return _check(text, new, want)
    if getattr(doc, "inline_empty", False):
        doc.lines[doc.head] = re.sub(r"\[\]", "", doc.lines[doc.head], count=1).rstrip() + "\n"
    at = doc.block_end
    doc.lines[at:at] = _dump([entry], doc.item_ind)
    return _check(text, doc.text(), want)


def delete(text: str, n: int) -> str:
    want = entries(text)
    if not 0 <= n < len(want):
        raise EditError("no such model entry")
    del want[n]
    doc = _Doc(text)
    s, e = doc.items[n]
    del doc.lines[s:e]
    return _check(text, doc.text(), want)


def update(text: str, n: int, entry: dict) -> str:
    """Change entry n to `entry`: only the keys whose value changed are rewritten (a scalar keeps its inline comment)."""
    old_list = entries(text)
    if not 0 <= n < len(old_list):
        raise EditError("no such model entry")
    old = old_list[n] if isinstance(old_list[n], dict) else {}
    want = list(old_list)
    want[n] = dict(entry)
    if old == entry:
        return text
    doc = _Doc(text)
    start, end = doc.items[n]
    try:
        keys = doc.keys(n)
    except EditError:
        if end - start != 1:
            raise
        # a one-line entry such as `- {model: x, provider: y}`: rewrite that line, keeping a comment after it
        m = re.search(r"\s+#.*$", doc.lines[start].rstrip("\n"))
        tail = m.group(0) if m and doc.lines[start].count("#") == 1 else ""
        new = _dump([entry], doc.item_ind)
        new[-1] = new[-1].rstrip("\n") + tail + "\n"
        doc.lines[start:end] = new
        return _check(text, doc.text(), want)
    kind = doc.item_ind + 2
    # apply bottom-up so earlier line numbers stay valid
    edits = []
    for k, (s, e) in keys.items():
        if k not in entry:
            edits.append((s, e, []))
        elif entry[k] != old.get(k):
            edits.append((s, e, _key_lines(doc, s, e, k, old.get(k), entry[k], kind)))
    for s, e, new_lines in sorted(edits, key=lambda x: -x[0]):
        doc.lines[s:e] = new_lines
    added = [k for k in entry if k not in keys]
    if added:
        e = _Doc(doc.text()).items[n][1]       # the entry's end moved with the edits above
        doc.lines[e:e] = _dump({k: entry[k] for k in added}, kind)
    # the entry's first line must start with "- " (its first key may have been removed)
    ln = doc.lines[start]
    if not ln.lstrip(" ").startswith("-"):
        doc.lines[start] = " " * doc.item_ind + "- " + ln[kind:]
    return _check(text, doc.text(), want)


def _key_lines(doc, s, e, key, old_val, new_val, kind):
    """The new lines for one key: a scalar on one line is changed in place (its inline comment kept),
    anything else is the key's block written afresh at the same place."""
    first = doc.lines[s]
    one_line = e == s + 1
    if one_line and not isinstance(new_val, (dict, list)) and not isinstance(old_val, (dict, list)):
        m = re.match(r"^(\s*(?:-\s+)?" + re.escape(key) + r"\s*:\s*)(.*?)(\s+#.*)?(\n?)$", first)
        if m:
            val = yaml.safe_dump(new_val, default_flow_style=True, allow_unicode=True, width=1000).strip()
            val = re.sub(r"\n?\.\.\.$", "", val).strip()
            return [m.group(1) + val + (m.group(3) or "") + "\n"]
    out = _dump({key: new_val}, kind)
    if first.lstrip(" ").startswith("-"):
        out[0] = " " * doc.item_ind + "- " + out[0][kind:]
    return out
