//! The session JSONL log (Python `aurora/session.py`, R20): one record per
//! line, `{"ts", "event", **data}`, never auto-deleted, rotated into
//! `<id>.2.jsonl`, `<id>.3.jsonl`, … past 5 MiB, each append under `flock`
//! (R170f) so a Python and a Rust writer on one session never interleave.

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};

use aurora_core::SessionLog;
use chrono::{Timelike, Utc};
use serde_json::{Map, Value};

use aurora_core::pyjson;
use crate::Result;

pub const SESSION_LOG_MAX_BYTES: u64 = 5 * 1024 * 1024;
const NEEDLE: &[u8] = b"\"event\": \"compact\"";

/// `datetime.now(UTC).isoformat()`: microseconds, and none at all when zero.
pub fn py_isoformat_now() -> String {
    let now = Utc::now();
    if now.nanosecond() / 1000 == 0 {
        now.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        now.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()
    }
}

/// A fresh id: 12 hex characters, like `uuid.uuid4().hex[:12]`.
pub fn new_session_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()[..12].to_string()
}

pub struct JsonlSession {
    id: String,
    dir: PathBuf,
    /// R570: the newest part seen (1 = the base file). Parts only grow forward, so an append looks from here on
    /// instead of probing every part from the first one again.
    newest: std::sync::Mutex<usize>,
}

impl JsonlSession {
    pub fn new(dir: PathBuf, id: Option<String>) -> Self {
        Self { id: id.unwrap_or_else(new_session_id), dir, newest: std::sync::Mutex::new(1) }
    }

    fn part_path(&self, n: usize) -> PathBuf {
        if n <= 1 { self.base_path() } else { self.dir.join(format!("{}.{n}.jsonl", self.id)) }
    }

    pub fn open(id: Option<String>) -> Result<Self> {
        Ok(Self::new(crate::paths::sessions_dir()?, id))
    }

    pub fn base_path(&self) -> PathBuf {
        self.dir.join(format!("{}.jsonl", self.id))
    }

    /// Every part, oldest first. Parts only grow forward, so probing 2, 3, …
    /// until one is missing is exact.
    pub fn parts(&self) -> Vec<PathBuf> {
        let mut out = Vec::new();
        let base = self.base_path();
        if base.exists() {
            out.push(base);
        }
        for n in 2.. {
            let p = self.dir.join(format!("{}.{n}.jsonl", self.id));
            if !p.exists() {
                break;
            }
            out.push(p);
        }
        out
    }

    fn write_target(&self) -> PathBuf {
        let mut newest = self.newest.lock().expect("session part lock");
        let mut n = *newest;
        while self.part_path(n + 1).exists() {   // another writer (the other loop) rotated meanwhile
            n += 1;
        }
        let current = self.part_path(n);
        let Ok(meta) = fs::metadata(&current) else {
            *newest = n;
            return current;
        };
        if meta.len() >= SESSION_LOG_MAX_BYTES {
            n += 1;
        }
        *newest = n;
        self.part_path(n)
    }

    pub fn append(&self, event: &str, data: Map<String, Value>) -> Result<()> {
        let mut rec = Map::new();
        rec.insert("ts".into(), Value::String(py_isoformat_now()));
        rec.insert("event".into(), Value::String(event.into()));
        for (k, v) in data {
            rec.insert(k, v);
        }
        let mut line = pyjson::dumps(&Value::Object(rec));
        line.push('\n');
        append_locked(&self.write_target(), line.as_bytes())
    }

    /// Every record across every part; a corrupt line is skipped, never fatal.
    pub fn records(&self) -> Vec<Value> {
        let mut out = Vec::new();
        for p in self.parts() {
            let Ok(f) = fs::File::open(&p) else { continue };
            for line in BufReader::new(f).split(b'\n').map_while(|l| l.ok()) {
                let text = String::from_utf8_lossy(&line);
                if text.trim().is_empty() {
                    continue;
                }
                if let Ok(v) = serde_json::from_str::<Value>(&text) {
                    out.push(v);
                }
            }
        }
        out
    }

    /// R20/R128/R170c (Python `Engine.resume_from`): the past conversation as
    /// flat `user`/`assistant` text messages. Same-role neighbours are merged,
    /// a dangling last `user` prompt is dropped. Returns the messages and the
    /// number of turns restored; `None` when nothing was restorable.
    pub fn restore(&self) -> Option<(Vec<Value>, usize)> {
        restore_messages(&self.records())
    }

    /// How many context folds this session's log holds (R159).
    /// R571: lines are prefiltered on the literal `"event": "compact"` (both loops write that spacing), so a long
    /// session is no longer parsed whole to count its folds.
    pub fn compactions(&self) -> usize {
        let mut n = 0;
        for p in self.parts() {
            let Ok(f) = fs::File::open(&p) else { continue };
            for line in BufReader::new(f).split(b'\n').map_while(|l| l.ok()) {
                if !line.windows(NEEDLE.len()).any(|w| w == NEEDLE) {
                    continue;
                }
                if serde_json::from_slice::<Value>(&line).is_ok_and(|v| v["event"] == "compact") {
                    n += 1;
                }
            }
        }
        n
    }
}

pub fn restore_messages(records: &[Value]) -> Option<(Vec<Value>, usize)> {
    let mut msgs: Vec<Value> = Vec::new();
    let mut restored: usize = 0;
    for r in records {
        let ev = r["event"].as_str().unwrap_or("");
        let text = r["text"].as_str().unwrap_or("");
        if !(ev == "user" || ev == "assistant") || text.is_empty() {
            continue;
        }
        if let Some(last) = msgs.last_mut() {
            if last["role"] == ev {
                let merged = format!("{}\n\n{}", last["content"].as_str().unwrap_or(""), text);
                last["content"] = Value::String(merged);
                continue;
            }
        }
        msgs.push(serde_json::json!({"role": ev, "content": text}));
        if ev == "user" {
            restored += 1;
        }
    }
    if restored == 0 {
        return None;
    }
    if msgs.last().is_some_and(|m| m["role"] == "user") {
        msgs.pop();
        restored -= 1;
    }
    Some((msgs, restored))
}

/// Python `_ROTATED_RE`: `<id>.<n>.jsonl` with n >= 2.
fn rotated_base(name: &str) -> Option<&str> {
    let stem = name.strip_suffix(".jsonl")?;
    let (base, n) = stem.rsplit_once('.')?;
    let ok = !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()) && !n.starts_with('0') && n != "1";
    (ok && !base.is_empty()).then_some(base)
}

/// Python `latest_session_id`: the session whose freshest part changed last.
pub fn latest_session_id(dir: &Path) -> Option<String> {
    let mut best: Option<(std::time::SystemTime, String)> = None;
    for e in fs::read_dir(dir).ok()?.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if !name.ends_with(".jsonl") {
            continue;
        }
        let sid = rotated_base(&name).map(str::to_string).unwrap_or_else(|| name[..name.len() - 6].to_string());
        let Ok(mt) = e.metadata().and_then(|m| m.modified()) else { continue };
        if best.as_ref().is_none_or(|(t, _)| mt > *t) {
            best = Some((mt, sid));
        }
    }
    best.map(|(_, s)| s)
}

fn append_locked(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut f = OpenOptions::new().create(true).append(true).open(path)?;
    let fd = f.as_raw_fd();
    // SAFETY: fd is a valid descriptor owned by `f` for this whole block.
    unsafe { libc::flock(fd, libc::LOCK_EX) };
    let res = f.write_all(bytes).and_then(|_| f.flush());
    unsafe { libc::flock(fd, libc::LOCK_UN) };
    Ok(res?)
}

impl SessionLog for JsonlSession {
    fn id(&self) -> &str {
        &self.id
    }

    fn log(&self, event: &str, data: Map<String, Value>) {
        if let Err(e) = self.append(event, data) {
            eprintln!("aurora: session log write failed: {e}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn obj(v: Value) -> Map<String, Value> {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn records_use_python_spacing_and_field_order() {
        let dir = tempfile::tempdir().unwrap();
        let s = JsonlSession::new(dir.path().into(), Some("abc123def456".into()));
        s.append("user", obj(json!({"text": "olá", "model": "m"}))).unwrap();
        let raw = fs::read_to_string(s.base_path()).unwrap();
        assert!(raw.starts_with(r#"{"ts": ""#));
        assert!(raw.contains(r#", "event": "user", "text": "olá", "model": "m"}"#));
        assert_eq!(s.records()[0]["text"], "olá");
    }

    #[test]
    fn rotates_past_the_cap_and_reads_every_part() {
        let dir = tempfile::tempdir().unwrap();
        let s = JsonlSession::new(dir.path().into(), Some("rot".into()));
        fs::write(s.base_path(), vec![b' '; SESSION_LOG_MAX_BYTES as usize]).unwrap();
        s.append("user", obj(json!({"text": "after"}))).unwrap();
        assert!(dir.path().join("rot.2.jsonl").exists());
        assert_eq!(s.records().len(), 1);
    }

    #[test]
    fn appends_follow_a_part_another_writer_rotated_to() {
        // R570: the cached newest part never hides a newer one
        let dir = tempfile::tempdir().unwrap();
        let s = JsonlSession::new(dir.path().into(), Some("two".into()));
        s.append("user", obj(json!({"text": "one"}))).unwrap();
        fs::write(dir.path().join("two.2.jsonl"), "").unwrap();
        fs::write(dir.path().join("two.3.jsonl"), "").unwrap();
        s.append("user", obj(json!({"text": "three"}))).unwrap();
        assert!(fs::read_to_string(dir.path().join("two.3.jsonl")).unwrap().contains("three"));
        fs::write(dir.path().join("two.3.jsonl"), vec![b' '; SESSION_LOG_MAX_BYTES as usize]).unwrap();
        s.append("user", obj(json!({"text": "four"}))).unwrap();
        assert!(fs::read_to_string(dir.path().join("two.4.jsonl")).unwrap().contains("four"));
        assert_eq!(s.parts().len(), 4);
    }

    #[test]
    fn compactions_count_only_compact_records() {
        // R571
        let dir = tempfile::tempdir().unwrap();
        let s = JsonlSession::new(dir.path().into(), Some("cmp".into()));
        s.append("user", obj(json!({"text": "say \"event\": \"compact\" please"}))).unwrap();
        s.append("compact", obj(json!({"folded": 3}))).unwrap();
        s.append("assistant", obj(json!({"text": "ok"}))).unwrap();
        s.append("compact", obj(json!({"folded": 2}))).unwrap();
        assert_eq!(s.compactions(), 2);
    }

    #[test]
    fn a_corrupt_line_is_skipped() {
        let dir = tempfile::tempdir().unwrap();
        let s = JsonlSession::new(dir.path().into(), Some("bad".into()));
        fs::write(s.base_path(), "{not json\n").unwrap();
        s.append("user", obj(json!({"text": "ok"}))).unwrap();
        assert_eq!(s.records().len(), 1);
    }

    #[test]
    fn ids_look_like_python_ones() {
        let id = new_session_id();
        assert_eq!(id.len(), 12);
        assert!(id.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn restore_merges_neighbours_and_drops_a_dangling_prompt() {
        let recs = vec![
            json!({"event": "user", "text": "a"}),
            json!({"event": "tool", "text": "x"}),
            json!({"event": "user", "text": "b"}),
            json!({"event": "assistant", "text": "c"}),
            json!({"event": "assistant", "text": ""}),
            json!({"event": "user", "text": "dangling"}),
        ];
        let (msgs, n) = restore_messages(&recs).unwrap();
        assert_eq!(n, 1);
        assert_eq!(msgs, vec![json!({"role": "user", "content": "a\n\nb"}), json!({"role": "assistant", "content": "c"})]);
        assert!(restore_messages(&[json!({"event": "tool"})]).is_none());
        // only a dangling prompt: still resumes the session, with no messages
        assert_eq!(restore_messages(&[json!({"event": "user", "text": "p"})]), Some((vec![], 0)));
    }

    #[test]
    fn latest_follows_rotated_parts() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("old.jsonl"), "{}\n").unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        fs::write(dir.path().join("aaa.jsonl"), "{}\n").unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        fs::write(dir.path().join("old.2.jsonl"), "{}\n").unwrap();
        assert_eq!(latest_session_id(dir.path()).as_deref(), Some("old"));
        assert_eq!(rotated_base("x.1.jsonl"), None);
        assert_eq!(rotated_base("x.10.jsonl"), Some("x"));
    }
}
