//! `apply_patch`'s engine (Python `aurora/patch.py`, R97): a unified diff
//! applied by content, all hunks or none. Also the R309 miss hints and the
//! R457 whitespace-tolerant match that `edit_file` shares.

#[derive(Debug, Clone, PartialEq)]
pub struct Hunk {
    pub old: String,
    pub new: String,
    pub header: String,
    /// R220: old lines differ from new lines (not the same as `old != new`).
    pub changed: bool,
}

/// Python `str.splitlines()`.
pub fn py_splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        let brk = matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}');
        if brk {
            out.push(&s[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r' && it.peek().is_some_and(|(_, n)| *n == '\n') {
                it.next();
                end += 1;
            }
            start = end;
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// Python `repr(str)` for the messages that quote a hunk header or a line.
pub fn py_repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

fn is_hunk_header(line: &str) -> bool {
    // ^@@ .* @@
    line.starts_with("@@ ") && line[3..].contains(" @@")
}

fn is_file_header(lines: &[&str], i: usize) -> bool {
    let line = lines[i];
    if line.starts_with("diff ") {
        return true;
    }
    if line.starts_with("--- ") {
        return lines.get(i + 1).is_some_and(|n| n.starts_with("+++ "));
    }
    if line.starts_with("+++ ") {
        return i > 0 && lines[i - 1].starts_with("--- ");
    }
    false
}

pub fn parse(diff: &str) -> Result<Vec<Hunk>, String> {
    let lines = py_splitlines(diff);
    let mut hunks = Vec::new();
    let mut i = 0;
    while i < lines.len() {
        if !is_hunk_header(lines[i]) {
            i += 1;
            continue;
        }
        let header = lines[i].to_string();
        i += 1;
        let (mut old, mut new): (Vec<&str>, Vec<&str>) = (Vec::new(), Vec::new());
        while i < lines.len() && !is_hunk_header(lines[i]) && !is_file_header(&lines, i) {
            let raw = lines[i];
            if raw.starts_with('\\') {
                i += 1;
                continue;
            }
            match raw.chars().next() {
                None => {
                    old.push("");
                    new.push("");
                }
                Some(' ') => {
                    old.push(&raw[1..]);
                    new.push(&raw[1..]);
                }
                Some('-') => old.push(&raw[1..]),
                Some('+') => new.push(&raw[1..]),
                Some(_) => {
                    return Err(format!(
                        "hunk {}: bad line (must start with ' ', '-', '+', or be blank): {}",
                        py_repr(&header),
                        py_repr(raw)
                    ))
                }
            }
            i += 1;
        }
        if old.is_empty() && new.is_empty() {
            return Err(format!("hunk {}: empty — nothing to apply", py_repr(&header)));
        }
        if old.is_empty() {
            return Err(format!(
                "hunk {}: pure insertion with no surrounding context — include at least one unchanged line before or after the insertion point",
                py_repr(&header)
            ));
        }
        hunks.push(Hunk { old: old.join("\n"), new: new.join("\n"), header, changed: old != new });
    }
    if hunks.is_empty() {
        return Err("no hunks found — expected a unified diff (\"@@ ... @@\" hunk headers)".into());
    }
    Ok(hunks)
}

/// R281: byte offsets where `old` matches as whole lines.
fn line_anchored(text: &str, old: &str) -> Vec<usize> {
    let mut out = Vec::new();
    let mut from = 0;
    while let Some(rel) = text[from..].find(old) {
        let i = from + rel;
        let end = i + old.len();
        let b = text.as_bytes();
        if (i == 0 || b[i - 1] == b'\n') && (end == text.len() || b[end] == b'\n') {
            out.push(i);
        }
        let step = text[i..].chars().next().map_or(1, char::len_utf8);
        from = i + step;
        if from > text.len() {
            break;
        }
    }
    out
}

fn squash(t: &str) -> String {
    let mut out = String::with_capacity(t.len());
    let mut in_run = false;
    for c in t.chars() {
        if c == ' ' || c == '\t' {
            if !in_run {
                out.push(' ');
            }
            in_run = true;
        } else {
            out.push(c);
            in_run = false;
        }
    }
    out
}

/// R309: why `old` missed, or "".
pub fn not_found_hint(text: &str, old: &str) -> String {
    let s = old.trim();
    if !s.is_empty() && text.contains(s) {
        return " (found with different leading/trailing whitespace)".into();
    }
    if squash(text).contains(&squash(old)) {
        return " (found with different indentation or spacing — tabs vs spaces?)".into();
    }
    let first = py_splitlines(old).into_iter().map(str::trim).find(|l| !l.is_empty()).unwrap_or("");
    if !first.is_empty() && py_splitlines(text).iter().filter(|l| l.trim() == first).count() == 1 {
        return " (the first line exists; a later line differs)".into();
    }
    String::new()
}

fn hunk_hint(text: &str, old: &str) -> String {
    if text.contains(old) {
        return " (the text exists, but only inside a longer line)".into();
    }
    not_found_hint(text, old)
}

pub fn apply(text: &str, hunks: &[Hunk]) -> Result<String, String> {
    let mut text = text.to_string();
    for h in hunks {
        if !h.changed {
            continue;
        }
        let hits = line_anchored(&text, &h.old);
        match hits.len() {
            0 => {
                return Err(format!(
                    "hunk {}: context not found{} — the file may have changed since the patch was written, or a line is off by one; re-read the file and regenerate the patch",
                    py_repr(&h.header),
                    hunk_hint(&text, &h.old)
                ))
            }
            1 => {}
            n => return Err(format!("hunk {}: context matches {n} times — add more surrounding lines to make it unique", py_repr(&h.header))),
        }
        let i = hits[0];
        text = format!("{}{}{}", &text[..i], h.new, &text[i + h.old.len()..]);
    }
    Ok(text)
}

fn ws_key(line: &str) -> String {
    line.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn indent(line: &str) -> &str {
    &line[..line.len() - line.trim_start_matches([' ', '\t']).len()]
}

/// R457: `old` matched once as whole lines with whitespace ignored → the edited text.
pub fn whitespace_edit(text: &str, old: &str, new: &str) -> Option<String> {
    let want: Vec<&str> = old.trim_matches('\n').split('\n').collect();
    let keys: Vec<String> = want.iter().map(|l| ws_key(l)).collect();
    if keys.iter().all(String::is_empty) {
        return None;
    }
    let lines: Vec<&str> = text.split('\n').collect();
    let n = want.len();
    if lines.len() < n {
        return None;
    }
    let hits: Vec<usize> =
        (0..=lines.len() - n).filter(|&i| lines[i..i + n].iter().map(|l| ws_key(l)).eq(keys.iter().cloned())).collect();
    if hits.len() != 1 {
        return None;
    }
    let i = hits[0];
    let k = keys.iter().position(|k| !k.is_empty())?;
    let (old_ind, file_ind) = (indent(want[k]), indent(lines[i + k]));
    let body = new.trim_matches('\n');
    let mut out: Vec<String> = Vec::new();
    if !body.is_empty() {
        for ln in body.split('\n') {
            let l = if !ln.trim().is_empty() && !old_ind.is_empty() && ln.starts_with(old_ind) {
                format!("{file_ind}{}", &ln[old_ind.len()..])
            } else if !ln.trim().is_empty() && old_ind.is_empty() && !file_ind.is_empty() {
                format!("{file_ind}{ln}")
            } else {
                ln.to_string()
            };
            out.push(l);
        }
    }
    let mut all: Vec<String> = lines[..i].iter().map(|s| s.to_string()).collect();
    all.extend(out);
    all.extend(lines[i + n..].iter().map(|s| s.to_string()));
    Some(all.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn applies_in_order_and_refuses_partial_matches() {
        let text = "a\ntotal_count = 5\ncount = 5\nb\n";
        let h = parse("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-count = 5\n+count = 6\n").unwrap();
        assert_eq!(apply(text, &h).unwrap(), "a\ntotal_count = 5\ncount = 6\nb\n");
        let h = parse("@@ -2 @@\n-otal_count = 5\n+x\n").unwrap();
        assert!(apply(text, &h).unwrap_err().contains("only inside a longer line"));
        assert!(parse("nothing").unwrap_err().starts_with("no hunks found"));
        assert!(parse("@@ -1 +1 @@\n+only\n").unwrap_err().contains("pure insertion"));
        // R236: a removed '---' line is content, not a file header
        let h = parse("@@ -1,2 +1 @@\n a\n----\n").unwrap();
        assert_eq!(apply("a\n---\nb", &h).unwrap(), "a\nb");
    }

    #[test]
    fn whitespace_edit_reindents() {
        let text = "def f():\n    if x:\n        return 1\n";
        let got = whitespace_edit(text, "if x:\n    return 1", "if y:\n    return 2").unwrap();
        assert_eq!(got, "def f():\n    if y:\n        return 2\n");
        assert_eq!(py_repr("it's"), "\"it's\"");
    }
}
