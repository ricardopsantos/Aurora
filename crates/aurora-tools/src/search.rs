//! `grep` and `find_files` (Python `tools.grep` / `tools.find_files`): the
//! same prune list, caps, timeouts and result strings.

use std::path::PathBuf;
use std::process::Stdio;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use aurora_core::text::head;
use aurora_core::{Args, CancellationToken, Tool, ToolSpec};
use aurora_storage::pathlike::fnmatch;
use serde_json::json;
use tokio::io::AsyncReadExt;

use crate::arg_str;
use crate::fs::{resolve, MAX_READ_BYTES};

pub const GREP_PRUNE: [&str; 10] =
    [".git", "node_modules", ".venv", "venv", "__pycache__", ".build", "build", "dist", ".mypy_cache", ".pytest_cache"];
pub const MAX_FIND_RESULTS: usize = 500;
pub const FIND_TIMEOUT: u64 = 30;
pub const GREP_TIMEOUT: u64 = 30;
const STDERR_CAP: usize = 4096;

/// `str(Path(p).expanduser())`: `.` and empty parts dropped, `""` is `.`.
pub fn py_path(path: &str) -> PathBuf {
    let p = resolve(if path.is_empty() { "." } else { path });
    let s = p.to_string_lossy();
    let abs = s.starts_with('/');
    let parts: Vec<&str> = s.split('/').filter(|c| !c.is_empty() && *c != ".").collect();
    let joined = parts.join("/");
    PathBuf::from(match (abs, joined.is_empty()) {
        (true, _) => format!("/{joined}"),
        (false, true) => ".".to_string(),
        (false, false) => joined,
    })
}

fn not_a_dir(path: &str) -> String {
    let cwd = std::env::current_dir().map(|d| d.display().to_string()).unwrap_or_default();
    format!("[error: not a directory: {path} — cwd is {cwd}; use an absolute path or ~/…]")
}

pub fn find_files(pattern: &str, path: &str, cancel: &dyn Fn() -> bool, timeout: Duration) -> String {
    let root = py_path(path);
    if !root.is_dir() {
        return not_a_dir(path);
    }
    let deadline = Instant::now() + timeout;
    let mut out: Vec<String> = Vec::new();
    let (mut truncated, mut stopped) = (false, "");
    // os.walk: top-down, a directory's files before its subdirectories
    let mut stack: Vec<PathBuf> = vec![root.clone()];
    'walk: while let Some(dir) = stack.pop() {
        if Instant::now() > deadline {
            stopped = "timed out";
            break;
        }
        if cancel() {
            stopped = "cancelled by user";
            break;
        }
        let Ok(rd) = std::fs::read_dir(&dir) else { continue };
        let mut subdirs = Vec::new();
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            // os.walk follows the entry's type without following symlinked dirs
            let is_dir = e.file_type().map(|t| t.is_dir()).unwrap_or(false)
                || (e.file_type().map(|t| t.is_symlink()).unwrap_or(false) && e.path().is_dir());
            if is_dir {
                if !GREP_PRUNE.contains(&name.as_str()) && !e.file_type().map(|t| t.is_symlink()).unwrap_or(false) {
                    subdirs.push(e.path());
                }
                continue;
            }
            let rel = e.path().strip_prefix(&root).map(|r| r.to_string_lossy().to_string()).unwrap_or(name.clone());
            if fnmatch(&name, pattern) || fnmatch(&rel, pattern) {
                out.push(rel);
                if out.len() >= MAX_FIND_RESULTS {
                    truncated = true;
                    break 'walk;
                }
            }
        }
        subdirs.sort();
        stack.extend(subdirs.into_iter().rev());
    }
    out.sort();
    if !stopped.is_empty() {
        let what = if stopped == "timed out" { format!("timed out after {}s", timeout.as_secs()) } else { stopped.to_string() };
        let note = format!("[search {what} — results are PARTIAL; narrow the path/pattern]");
        return if out.is_empty() { note } else { format!("{}\n{note}", out.join("\n")) };
    }
    if out.is_empty() {
        return "[no matches]".into();
    }
    let mut text = out.join("\n");
    if truncated {
        text.push_str(&format!("\n[truncated at {MAX_FIND_RESULTS} results — narrow the pattern/path]"));
    }
    text
}

pub async fn grep(pattern: &str, path: &str, timeout: Duration) -> String {
    let mut cmd = tokio::process::Command::new("grep");
    cmd.arg("-rnIE");
    for d in GREP_PRUNE {
        cmd.arg(format!("--exclude-dir={d}"));
    }
    cmd.args(crate::guard::grep_excludes()); // R450: web mode only
    cmd.arg("--").arg(pattern).arg(py_path(path));
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(true);
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return format!("[grep error: {e}]"),
    };
    let (Some(mut so), Some(mut se)) = (child.stdout.take(), child.stderr.take()) else {
        return "[grep error: no pipes]".into();
    };
    // stderr is drained alongside so a flood of "Permission denied" never blocks grep
    let err_task = tokio::spawn(async move {
        let (mut kept, mut buf) = (Vec::new(), [0u8; 65536]);
        while let Ok(n) = se.read(&mut buf).await {
            if n == 0 {
                break;
            }
            if kept.len() < STDERR_CAP {
                kept.extend_from_slice(&buf[..n]);
            }
        }
        kept
    });
    let mut data = Vec::new();
    let mut truncated = false;
    let read = async {
        let mut buf = [0u8; 65536];
        loop {
            match so.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    data.extend_from_slice(&buf[..n]);
                    if data.len() >= MAX_READ_BYTES {
                        truncated = true;
                        break;
                    }
                }
            }
        }
    };
    let timed_out = tokio::time::timeout(timeout, read).await.is_err();
    if timed_out || truncated {
        let _ = child.start_kill();
    }
    let status = tokio::time::timeout(Duration::from_secs(5), child.wait()).await.ok().and_then(Result::ok);
    let stderr = tokio::time::timeout(Duration::from_secs(1), err_task).await.ok().and_then(Result::ok).unwrap_or_default();
    if timed_out {
        return format!("[grep error: timeout after {}s]", timeout.as_secs());
    }
    let out = String::from_utf8_lossy(&data);
    let out = out.trim();
    if !out.is_empty() {
        if truncated {
            return format!("{}\n[output truncated at {MAX_READ_BYTES} chars — narrow the pattern/path]", head(out, MAX_READ_BYTES));
        }
        return out.to_string();
    }
    let code = status.and_then(|s| s.code()).unwrap_or(2);
    if code >= 2 {
        let err = String::from_utf8_lossy(&stderr);
        let detail = err.trim().lines().next().map(str::to_string).unwrap_or_else(|| format!("exit {code}"));
        return format!("[grep error: {detail}]");
    }
    "[no matches]".into()
}

pub struct Grep;
pub struct FindFiles;

#[async_trait]
impl Tool for Grep {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "grep".into(),
            description: "Recursively search for an extended regex (ERE) or plain string; returns file:line matches.".into(),
            parameters: json!({"type": "object", "properties": {
                "pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}),
        }
    }
    fn needs_approval(&self, _: &Args) -> bool {
        false
    }
    fn parallel_safe(&self) -> bool {
        true
    }
    async fn run(&self, args: &Args, _: &CancellationToken) -> String {
        grep(arg_str(args, "pattern"), arg_str(args, "path"), Duration::from_secs(GREP_TIMEOUT)).await
    }
}

#[async_trait]
impl Tool for FindFiles {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "find_files".into(),
            description: "Recursively search for files by GLOB pattern (e.g. '*.py', 'test_*.md') — matches by filename, not content; use `grep` to search inside files.".into(),
            parameters: json!({"type": "object", "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "default '.'"}}, "required": ["pattern"]}),
        }
    }
    fn needs_approval(&self, _: &Args) -> bool {
        false
    }
    fn parallel_safe(&self) -> bool {
        true
    }
    async fn run(&self, args: &Args, cancel: &CancellationToken) -> String {
        let (pattern, path, c) = (arg_str(args, "pattern").to_string(), arg_str(args, "path").to_string(), cancel.clone());
        tokio::task::spawn_blocking(move || find_files(&pattern, &path, &|| c.is_cancelled(), Duration::from_secs(FIND_TIMEOUT)))
            .await
            .unwrap_or_else(|e| format!("[tool error: find_files: {e}]"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tree() -> tempfile::TempDir {
        let d = tempfile::tempdir().unwrap();
        for f in ["a.py", "src/b.py", "src/c.txt", "node_modules/x.py", "tests/test_a.py"] {
            let p = d.path().join(f);
            fs::create_dir_all(p.parent().unwrap()).unwrap();
            fs::write(p, "hello world\nfoo(bar)\n").unwrap();
        }
        d
    }

    #[test]
    fn finds_by_name_and_relative_path_and_prunes() {
        let d = tree();
        let root = d.path().to_str().unwrap();
        let never = || false;
        let t = Duration::from_secs(30);
        assert_eq!(find_files("*.py", root, &never, t), "a.py\nsrc/b.py\ntests/test_a.py");
        assert_eq!(find_files("tests/test_*.py", root, &never, t), "tests/test_a.py");
        assert_eq!(find_files("*.rs", root, &never, t), "[no matches]");
        assert!(find_files("*", "/nonexistent-x", &never, t).starts_with("[error: not a directory: /nonexistent-x"));
        assert!(find_files("*", root, &|| true, t).starts_with("[search cancelled by user"));
    }

    #[tokio::test]
    async fn grep_uses_ere_and_reports_errors() {
        let d = tree();
        let root = d.path().to_str().unwrap();
        let t = Duration::from_secs(30);
        let out = grep("foo\\(bar|nothing", root, t).await;
        assert_eq!(out.lines().count(), 4, "{out}");
        assert!(!out.contains("node_modules"));
        assert_eq!(grep("zzz", root, t).await, "[no matches]");
        assert!(grep("(", root, t).await.starts_with("[grep error: "));
    }

    #[test]
    fn paths_print_like_pathlib() {
        assert_eq!(py_path(""), PathBuf::from("."));
        assert_eq!(py_path("./a//b/"), PathBuf::from("a/b"));
        assert_eq!(py_path("/x/./y"), PathBuf::from("/x/y"));
    }
}
