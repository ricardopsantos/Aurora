//! File tools (Python `read_file`, `list_dir`, `write_file`, `edit_file`).

use std::fs;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use async_trait::async_trait;
use aurora_core::text::{char_len, head, tail};
use aurora_core::{Args, CancellationToken, Tool, ToolSpec};
use serde_json::json;

use crate::{arg_bool, arg_int, arg_str};

pub const MAX_READ_BYTES: usize = 200_000;

pub fn resolve(path: &str) -> PathBuf {
    let home = || std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
    if path == "~" {
        home()
    } else if let Some(rest) = path.strip_prefix("~/") {
        home().join(rest)
    } else {
        PathBuf::from(path)
    }
}

fn lossy(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

/// The last `n` lines and the file's line count (Python `_tail_lines`).
fn tail_lines(p: &Path, n: usize) -> std::io::Result<(Vec<String>, usize)> {
    let mut f = fs::File::open(p)?;
    let size = f.metadata()?.len();
    let mut buf: Vec<u8> = Vec::new();
    let mut pos = size;
    let block = 1u64 << 16;
    // read backwards until n+1 newlines are in hand (or the start is reached)
    while pos > 0 && buf.iter().filter(|&&b| b == b'\n').count() <= n {
        let step = block.min(pos);
        pos -= step;
        f.seek(SeekFrom::Start(pos))?;
        let mut chunk = vec![0u8; step as usize];
        f.read_exact(&mut chunk)?;
        chunk.extend_from_slice(&buf);
        buf = chunk;
    }
    let text = lossy(&buf);
    let mut lines: Vec<String> = text.split_inclusive('\n').map(str::to_string).collect();
    if pos > 0 && !lines.is_empty() {
        lines.remove(0); // a partial first line
    }
    let keep = lines.len().saturating_sub(n);
    let lines = lines.split_off(keep);
    let total = if pos == 0 { text.split_inclusive('\n').count() } else { 0 };
    Ok((lines, total))
}

/// R525: whether the file's last byte is a newline, read with one seek.
fn ends_with_newline(p: &Path) -> std::io::Result<bool> {
    use std::io::{Seek, SeekFrom};
    let mut f = fs::File::open(p)?;
    let len = f.metadata()?.len();
    if len == 0 {
        return Ok(false);
    }
    f.seek(SeekFrom::Start(len - 1))?;
    let mut b = [0u8; 1];
    f.read_exact(&mut b)?;
    Ok(b[0] == b'\n')
}

pub fn read_file(path: &str, offset: i64, limit: i64, tail_n: i64) -> String {
    let p = resolve(path);
    if !p.is_file() {
        return format!("[error: no such file: {path}]");
    }
    if tail_n > 0 {
        let Ok((lines, total)) = tail_lines(&p, tail_n as usize) else {
            return format!("[error: cannot read {path}]");
        };
        if lines.is_empty() {
            return "[no lines: file is empty]".into();
        }
        let text: String = lines.concat();
        let text = if char_len(&text) > MAX_READ_BYTES { tail(&text, MAX_READ_BYTES).to_string() } else { text };
        let where_ = if total > 0 {
            format!("[last {} lines of {total}]", lines.len())
        } else {
            format!("[last {} lines]", lines.len())
        };
        return format!("{where_}\n{text}");
    }
    if offset != 0 || limit != 0 {
        let start = offset.max(1) as usize;
        let limit = limit.max(0) as usize;
        let Ok(f) = fs::File::open(&p) else { return format!("[error: cannot read {path}]") };
        let mut out = String::new();
        let (mut n, mut total, mut more, mut size) = (0usize, 0usize, false, 0usize);
        for (i, line) in BufReader::new(f).split(b'\n').enumerate() {
            let Ok(mut bytes) = line else { break };
            let lineno = i + 1;
            total = lineno;
            if lineno < start {
                continue;
            }
            if limit > 0 && n >= limit || size >= MAX_READ_BYTES {
                more = true;
                break;
            }
            bytes.push(b'\n');
            let s = lossy(&bytes);
            size += char_len(&s);
            out.push_str(&s);
            n += 1;
        }
        if n == 0 {
            return format!("[no lines: file has fewer than {start} lines]");
        }
        // a last line without a newline must not gain one
        // R525: the last byte alone (a seek), not the whole file read again
        if !more && !ends_with_newline(&p).unwrap_or(true) {
            out.pop();
        }
        let end = start + n - 1;
        if char_len(&out) > MAX_READ_BYTES {
            out = format!("{}\n[truncated at {MAX_READ_BYTES} bytes]", head(&out, MAX_READ_BYTES));
        }
        let where_ = if more { format!("[lines {start}-{end}, more follow]") } else { format!("[lines {start}-{end} of {total}]") };
        return format!("{where_}\n{out}");
    }
    let mut data = Vec::with_capacity(MAX_READ_BYTES + 1);
    if fs::File::open(&p).and_then(|f| f.take(MAX_READ_BYTES as u64 + 1).read_to_end(&mut data)).is_err() {
        return format!("[error: cannot read {path}]");
    }
    let mut text = lossy(&data[..data.len().min(MAX_READ_BYTES)]);
    if data.len() > MAX_READ_BYTES {
        text.push_str(&format!(
            "\n[truncated at {MAX_READ_BYTES} bytes — re-read with offset/limit for a specific line range]"
        ));
    }
    text
}

pub fn list_dir(path: &str) -> String {
    let path = if path.is_empty() { "." } else { path };
    let p = resolve(path);
    if !p.is_dir() {
        let cwd = std::env::current_dir().map(|d| d.display().to_string()).unwrap_or_default();
        return format!("[error: not a directory: {path} — cwd is {cwd}; use an absolute path or ~/…]");
    }
    let Ok(rd) = fs::read_dir(&p) else { return format!("[error: cannot list {path}]") };
    let mut entries: Vec<(String, bool)> =
        rd.filter_map(Result::ok).map(|e| (e.file_name().to_string_lossy().into_owned(), e.path().is_dir())).collect();
    entries.sort();
    if entries.is_empty() {
        return "[empty]".into();
    }
    entries.iter().map(|(n, d)| format!("{} {n}", if *d { 'd' } else { '-' })).collect::<Vec<_>>().join("\n")
}

/// Write keeping an existing file's permission bits (Python
/// `write_text_preserving`), via a sibling temp file and a rename.
fn write_preserving(p: &Path, text: &str) -> std::io::Result<()> {
    let mode = fs::metadata(p).ok().map(|m| m.permissions().mode());
    let dir = p.parent().filter(|d| !d.as_os_str().is_empty()).unwrap_or(Path::new("."));
    fs::create_dir_all(dir)?;
    let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("file");
    let tmp = dir.join(format!(".{name}.{}.tmp", std::process::id()));
    fs::write(&tmp, text)?;
    if let Some(m) = mode {
        fs::set_permissions(&tmp, fs::Permissions::from_mode(m))?;
    }
    fs::rename(&tmp, p).inspect_err(|_| {
        let _ = fs::remove_file(&tmp);
    })
}

pub fn write_file(path: &str, content: &str) -> String {
    match write_preserving(&resolve(path), content) {
        Ok(()) => format!("[wrote {} bytes to {path}]", char_len(content)),
        Err(e) => format!("[error: cannot write {path}: {e}]"),
    }
}

pub fn edit_file(path: &str, old: &str, new: &str, replace_all: bool) -> String {
    let p = resolve(path);
    if !p.is_file() {
        return format!("[error: no such file: {path}]");
    }
    if old.is_empty() {
        return "[error: `old` must not be empty — use write_file to create or replace a whole file]".into();
    }
    let Ok(raw) = fs::read_to_string(&p) else { return format!("[error: cannot read {path} as UTF-8]") };
    // R269: a uniformly-CRLF file is edited in LF form and converted back
    let crlf = raw.contains("\r\n") && raw.matches('\n').count() == raw.matches("\r\n").count();
    let text = if crlf { raw.replace("\r\n", "\n") } else { raw };
    let n = text.matches(old).count();
    if n == 0 {
        if let Some(fixed) = crate::patch::whitespace_edit(&text, old, new) {
            // R457
            let fixed = if crlf { fixed.replace('\n', "\r\n") } else { fixed };
            if let Err(e) = write_preserving(&p, &fixed) {
                return format!("[error: cannot write {path}: {e}]");
            }
            return format!("[edited {path} — `old` matched once with different indentation/spacing; `new` was re-indented to match]");
        }
        return format!("[error: `old` text not found — it must match exactly{}]", crate::patch::not_found_hint(&text, old));
    }
    if n > 1 && !replace_all {
        return format!("[error: `old` text appears {n} times — make it unique, or pass replace_all=true to change all {n}]");
    }
    let edited = text.replace(old, new);
    let edited = if crlf { edited.replace('\n', "\r\n") } else { edited };
    if let Err(e) = write_preserving(&p, &edited) {
        return format!("[error: cannot write {path}: {e}]");
    }
    if n == 1 { format!("[edited {path}]") } else { format!("[edited {path} — {n} occurrences]") }
}

/// Python `tools._read_for_edit` (R269): the text, LF-normalised when uniformly CRLF.
fn read_for_edit(p: &Path) -> std::io::Result<(String, bool)> {
    let raw = fs::read_to_string(p)?;
    let crlf = raw.contains("\r\n") && raw.matches('\n').count() == raw.matches("\r\n").count();
    Ok((if crlf { raw.replace("\r\n", "\n") } else { raw }, crlf))
}

/// R97: one unified diff, every hunk or none.
pub fn apply_patch(path: &str, diff: &str) -> String {
    let p = resolve(path);
    if !p.is_file() {
        return format!("[error: no such file: {path}]");
    }
    let hunks = match crate::patch::parse(diff) {
        Ok(h) => h,
        Err(e) => return format!("[error: {e}]"),
    };
    let Ok((text, crlf)) = read_for_edit(&p) else { return format!("[error: cannot read {path} as UTF-8]") };
    let new_text = match crate::patch::apply(&text, &hunks) {
        Ok(t) => t,
        Err(e) => return format!("[error: {e}]"),
    };
    if new_text == text {
        return "[no changes — patch was a no-op]".into();
    }
    let out = if crlf { new_text.replace('\n', "\r\n") } else { new_text };
    if let Err(e) = write_preserving(&p, &out) {
        return format!("[error: cannot write {path}: {e}]");
    }
    format!("[applied {} hunk(s) to {path}]", hunks.len())
}

fn spec(name: &str, description: &str, parameters: serde_json::Value) -> ToolSpec {
    ToolSpec { name: name.into(), description: description.into(), parameters }
}

pub struct ReadFile;
pub struct ListDir;
pub struct WriteFile;
pub struct EditFile;
pub struct ApplyPatch;

#[async_trait]
impl Tool for ReadFile {
    fn spec(&self) -> ToolSpec {
        spec("read_file", "Read a UTF-8 text file, optionally a line range.", json!({"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-based first line to read (optional)"},
            "limit": {"type": "integer", "description": "how many lines to read from offset (optional)"},
            "tail": {"type": "integer", "description": "read only the LAST n lines (fast on big logs; optional)"}},
            "required": ["path"]}))
    }
    fn needs_approval(&self, _: &Args) -> bool {
        false
    }
    fn parallel_safe(&self) -> bool {
        true
    }
    async fn run(&self, args: &Args, _: &CancellationToken) -> String {
        let (Ok(o), Ok(l), Ok(t)) = (arg_int(args, "offset"), arg_int(args, "limit"), arg_int(args, "tail")) else {
            return "[error: offset/limit/tail must be integers]".into();
        };
        let path = arg_str(args, "path").to_string();
        tokio::task::spawn_blocking(move || read_file(&path, o, l, t)).await.unwrap_or_else(|e| format!("[tool error: read_file: {e}]"))
    }
}

#[async_trait]
impl Tool for ListDir {
    fn spec(&self) -> ToolSpec {
        spec("list_dir", "List a directory's entries.",
             json!({"type": "object", "properties": {"path": {"type": "string", "description": "default '.'"}}, "required": []}))
    }
    fn needs_approval(&self, _: &Args) -> bool {
        false
    }
    fn parallel_safe(&self) -> bool {
        true
    }
    async fn run(&self, args: &Args, _: &CancellationToken) -> String {
        let path = arg_str(args, "path").to_string();
        tokio::task::spawn_blocking(move || list_dir(&path)).await.unwrap_or_else(|e| format!("[tool error: list_dir: {e}]"))
    }
}

#[async_trait]
impl Tool for WriteFile {
    fn spec(&self) -> ToolSpec {
        spec("write_file", "Create or overwrite a file with content (asks approval).", json!({"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}))
    }
    fn needs_approval(&self, _: &Args) -> bool {
        true
    }
    fn preview(&self, args: &Args) -> String {
        let path = arg_str(args, "path");
        let content = arg_str(args, "content");
        let verb = if resolve(path).exists() { "overwrite" } else { "create" };
        let shown: Vec<&str> = content.lines().take(20).collect();
        let more = content.lines().count().saturating_sub(shown.len());
        let mut out = format!("{verb} {path} ({} chars)\n", char_len(content));
        for l in shown {
            out.push_str(&format!("+{l}\n"));
        }
        if more > 0 {
            out.push_str(&format!("… {more} more lines\n"));
        }
        out
    }
    async fn run(&self, args: &Args, _: &CancellationToken) -> String {
        if !args.contains_key("path") || !args.contains_key("content") {
            return "[tool error: write_file: TypeError: write_file needs path and content]".into();
        }
        let (path, content) = (arg_str(args, "path").to_string(), arg_str(args, "content").to_string());
        tokio::task::spawn_blocking(move || write_file(&path, &content)).await.unwrap_or_else(|e| format!("[tool error: write_file: {e}]"))
    }
}

#[async_trait]
impl Tool for EditFile {
    fn spec(&self) -> ToolSpec {
        spec("edit_file", "Replace one unique occurrence of `old` with `new` in a file (asks approval).", json!({"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
            "replace_all": {"type": "boolean", "description": "replace every occurrence instead of requiring a unique match (optional)"}},
            "required": ["path", "old", "new"]}))
    }
    fn needs_approval(&self, _: &Args) -> bool {
        true
    }
    fn preview(&self, args: &Args) -> String {
        let minus: String = arg_str(args, "old").lines().map(|l| format!("-{l}\n")).collect();
        let plus: String = arg_str(args, "new").lines().map(|l| format!("+{l}\n")).collect();
        format!("{}\n{minus}{plus}", arg_str(args, "path"))
    }
    async fn run(&self, args: &Args, _: &CancellationToken) -> String {
        let path = arg_str(args, "path").to_string();
        let (old, new) = (arg_str(args, "old").to_string(), arg_str(args, "new").to_string());
        let all = arg_bool(args, "replace_all");
        tokio::task::spawn_blocking(move || edit_file(&path, &old, &new, all)).await.unwrap_or_else(|e| format!("[tool error: edit_file: {e}]"))
    }
}

#[async_trait]
impl Tool for ApplyPatch {
    fn spec(&self) -> ToolSpec {
        spec("apply_patch", "Apply a unified diff (like `git diff`/`diff -u` output) to a file — one call for several edits at once, instead of one edit_file call per change. Each hunk's context lines are matched by CONTENT, not the diff's line numbers, so approximate line numbers are fine. All hunks apply, or none do (asks approval).",
             json!({"type": "object", "properties": {
                 "path": {"type": "string"},
                 "diff": {"type": "string", "description": "unified diff hunks for this file — '@@ ... @@' headers followed by context (' '), removed ('-'), and added ('+') lines; --- / +++ file headers are optional and ignored (this `path` is authoritative)"}},
                 "required": ["path", "diff"]}))
    }
    fn needs_approval(&self, _: &Args) -> bool {
        true
    }
    /// R97: what the patch will really do, or why it will fail, never the raw diff alone.
    fn preview(&self, args: &Args) -> String {
        let path = arg_str(args, "path");
        let p = resolve(path);
        if !p.is_file() {
            return "[error: no such file — apply_patch requires an existing file]".into();
        }
        let result = read_for_edit(&p)
            .map_err(|e| e.to_string())
            .and_then(|(text, _)| crate::patch::parse(arg_str(args, "diff")).and_then(|h| crate::patch::apply(&text, &h)).map(|new| (text, new)));
        match result {
            Err(e) => format!("[patch will fail: {e}]"),
            Ok((text, new)) => {
                let (a, b): (Vec<&str>, Vec<&str>) = (text.split('\n').collect(), new.split('\n').collect());
                let pre = a.iter().zip(&b).take_while(|(x, y)| x == y).count();
                let suf = a[pre..].iter().rev().zip(b[pre..].iter().rev()).take_while(|(x, y)| x == y).count();
                let minus: String = a[pre..a.len() - suf].iter().map(|l| format!("-{l}\n")).collect();
                let plus: String = b[pre..b.len() - suf].iter().map(|l| format!("+{l}\n")).collect();
                format!("{path}\n@@ line {} @@\n{minus}{plus}", pre + 1)
            }
        }
    }
    async fn run(&self, args: &Args, _: &CancellationToken) -> String {
        let (path, diff) = (arg_str(args, "path").to_string(), arg_str(args, "diff").to_string());
        tokio::task::spawn_blocking(move || apply_patch(&path, &diff)).await.unwrap_or_else(|e| format!("[tool error: apply_patch: {e}]"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpfile(content: &str) -> (tempfile::TempDir, String) {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("f.txt");
        fs::write(&p, content).unwrap();
        (d, p.to_string_lossy().into())
    }

    #[test]
    fn read_whole_range_and_tail() {
        let (_d, p) = tmpfile("a\nb\nc\nd\n");
        assert_eq!(read_file(&p, 0, 0, 0), "a\nb\nc\nd\n");
        assert_eq!(read_file(&p, 2, 2, 0), "[lines 2-3, more follow]\nb\nc\n");
        assert_eq!(read_file(&p, 3, 0, 0), "[lines 3-4 of 4]\nc\nd\n");
        assert_eq!(read_file(&p, 9, 0, 0), "[no lines: file has fewer than 9 lines]");
        assert_eq!(read_file(&p, 0, 0, 2), "[last 2 lines of 4]\nc\nd\n");
        assert_eq!(read_file("/nonexistent/x", 0, 0, 0), "[error: no such file: /nonexistent/x]");
    }

    #[test]
    fn big_files_are_cut_with_the_python_notice() {
        let (_d, p) = tmpfile(&"x".repeat(MAX_READ_BYTES + 10));
        assert!(read_file(&p, 0, 0, 0).ends_with("[truncated at 200000 bytes — re-read with offset/limit for a specific line range]"));
    }

    #[test]
    fn list_marks_directories() {
        let d = tempfile::tempdir().unwrap();
        fs::create_dir(d.path().join("sub")).unwrap();
        fs::write(d.path().join("a.txt"), "").unwrap();
        assert_eq!(list_dir(&d.path().to_string_lossy()), "- a.txt\nd sub");
        let e = tempfile::tempdir().unwrap();
        assert_eq!(list_dir(&e.path().to_string_lossy()), "[empty]");
    }

    #[test]
    fn write_keeps_mode_and_edit_keeps_crlf() {
        let (_d, p) = tmpfile("one\r\ntwo\r\n");
        fs::set_permissions(&p, fs::Permissions::from_mode(0o750)).unwrap();
        assert_eq!(edit_file(&p, "one\ntwo", "uno\ndos", false), format!("[edited {p}]"));
        assert_eq!(fs::read_to_string(&p).unwrap(), "uno\r\ndos\r\n");
        assert_eq!(fs::metadata(&p).unwrap().permissions().mode() & 0o777, 0o750);
        assert_eq!(write_file(&p, "olá"), format!("[wrote 3 bytes to {p}]"));
        assert_eq!(fs::metadata(&p).unwrap().permissions().mode() & 0o777, 0o750);
    }

    #[test]
    fn edit_refuses_ambiguity_and_misses() {
        let (_d, p) = tmpfile("x x x");
        assert!(edit_file(&p, "x", "y", false).contains("appears 3 times"));
        assert_eq!(edit_file(&p, "x", "y", true), format!("[edited {p} — 3 occurrences]"));
        assert!(edit_file(&p, "zzz", "y", false).starts_with("[error: `old` text not found"));
    }
}
