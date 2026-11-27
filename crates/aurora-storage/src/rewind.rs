//! Checkpoints (Python `aurora/rewind.py`, R47): the turn-time half. Before
//! every approved mutation the working tree is snapshotted into a shadow git
//! repository (`AURORA_HOME/checkpoints/<sha1(cwd)[:16]>`, work tree = the
//! cwd), and the call's single target file, if it has one, is copied into
//! `last-mutation.json` (R181). Python's `/rewind`, `/undo` and `/diff` read
//! both, so changes made by either Aurora can be undone by either. Like
//! Python, nothing here may break a turn: failures are swallowed.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use serde_json::json;

pub const EXCLUDES: [&str; 11] = [
    "node_modules/", ".venv/", "venv/", "__pycache__/", ".build/", "build/", "dist/", ".mypy_cache/", ".pytest_cache/", "*.pyc", ".DS_Store",
];
pub const MAX_LABEL: usize = 72;
pub const MAX_CHECKPOINT_FILE_BYTES: u64 = 50 * 1024 * 1024;
pub const MAX_SNAPSHOT_BYTES: u64 = 20 * 1024 * 1024;
pub const RETENTION: usize = 200;
pub const UNDO_TAG_RETENTION: usize = 5;
const PRUNE_EVERY: u32 = 50;
const CWD_MARK: &str = "aurora-cwd";
pub const STALE_DAYS: u64 = 90;

static REPO_LOCK: Mutex<()> = Mutex::new(());
static SINCE_PRUNE: AtomicU32 = AtomicU32::new(0);
static LAST_SWEEP: AtomicU64 = AtomicU64::new(0);

/// SHA-1, for the shadow repo's name only (Python `hashlib.sha1(str(cwd))`).
pub fn sha1_hex(data: &[u8]) -> String {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let mut msg = data.to_vec();
    let bits = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bits.to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([chunk[4 * i], chunk[4 * i + 1], chunk[4 * i + 2], chunk[4 * i + 3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5A827999),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let t = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(*wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = t;
        }
        for (x, y) in h.iter_mut().zip([a, b, c, d, e]) {
            *x = x.wrapping_add(y);
        }
    }
    h.iter().map(|x| format!("{x:08x}")).collect()
}

pub struct Rewind {
    home: PathBuf,
    /// The resolved cwd: the work tree.
    wt: PathBuf,
}

fn now_s() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

impl Rewind {
    pub fn new(home: PathBuf, cwd: &Path) -> Self {
        let wt = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
        Self { home, wt }
    }

    fn gitdir_for(&self, wt: &Path) -> PathBuf {
        self.home.join("checkpoints").join(&sha1_hex(wt.to_string_lossy().as_bytes())[..16])
    }

    pub fn gitdir(&self) -> PathBuf {
        self.gitdir_for(&self.wt)
    }

    fn git(&self, args: &[&str]) -> std::io::Result<Output> {
        Command::new("git")
            .arg("--git-dir")
            .arg(self.gitdir())
            .arg("--work-tree")
            .arg(&self.wt)
            .args(["-c", "user.name=aurora", "-c", "user.email=aurora@localhost", "-c", "commit.gpgsign=false"])
            .args(args)
            .current_dir(&self.wt)
            .stdin(std::process::Stdio::null())
            .output()
    }

    fn too_broad(&self) -> bool {
        let home = std::env::var_os("HOME").map(PathBuf::from).and_then(|h| fs::canonicalize(h).ok());
        home.as_deref() == Some(self.wt.as_path()) || self.wt == Path::new("/")
    }

    fn ensure(&self) -> std::io::Result<()> {
        let gd = self.gitdir();
        if gd.join("HEAD").exists() && !gd.join(CWD_MARK).exists() {
            let _ = fs::write(gd.join(CWD_MARK), self.wt.to_string_lossy().as_bytes());
        }
        if !gd.join("HEAD").exists() {
            fs::create_dir_all(&gd)?;
            self.git(&["init", "--quiet"])?;
            fs::create_dir_all(gd.join("info"))?;
            fs::write(gd.join("info").join("exclude"), EXCLUDES.join("\n") + "\n")?;
            fs::write(gd.join(CWD_MARK), self.wt.to_string_lossy().as_bytes())?;
        }
        Ok(())
    }

    fn exclude_large_files(&self) {
        let info = self.gitdir().join("info");
        let record = info.join("aurora-large");
        let prev: Vec<String> =
            fs::read_to_string(&record).map(|t| t.split('\0').filter(|p| !p.is_empty()).map(String::from).collect()).unwrap_or_default();
        let mut cands: std::collections::BTreeSet<String> = prev.iter().cloned().collect();
        if let Ok(o) = self.git(&["status", "--porcelain", "-z", "-uall", "--no-renames"]) {
            for entry in String::from_utf8_lossy(&o.stdout).split('\0') {
                if entry.chars().count() > 3 {
                    cands.insert(entry.chars().skip(3).collect());
                }
            }
        }
        let big: Vec<String> = cands
            .into_iter()
            .filter(|rel| fs::metadata(self.wt.join(rel)).map(|m| m.len() > MAX_CHECKPOINT_FILE_BYTES).unwrap_or(false))
            .collect();
        let mut sorted_prev = prev.clone();
        sorted_prev.sort();
        let newly: Vec<&String> = big.iter().filter(|b| !prev.contains(b)).collect();
        if big != sorted_prev {
            let _ = fs::write(&record, big.join("\0"));
            let mut lines: Vec<String> = EXCLUDES.iter().map(|s| s.to_string()).collect();
            lines.extend(big.iter().map(|b| format!("/{}", gitignore_literal(b))));
            let _ = fs::write(info.join("exclude"), lines.join("\n") + "\n");
        }
        if !newly.is_empty() {
            let mut args = vec!["rm", "--cached", "-q", "--ignore-unmatch", "--"];
            args.extend(newly.iter().map(|s| s.as_str()));
            let _ = self.git(&args);
        }
    }

    /// Snapshot the tree; the short hash, or None when unchanged or impossible.
    pub fn checkpoint(&self, label: &str) -> Option<String> {
        if self.too_broad() {
            return None; // R275
        }
        self.ensure().ok()?;
        let short = {
            let _g = REPO_LOCK.lock().ok()?;
            self.exclude_large_files();
            self.git(&["add", "-A", "--ignore-errors"]).ok()?;
            let msg: String = label.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(MAX_LABEL).collect();
            let msg = if msg.is_empty() { "checkpoint".to_string() } else { msg };
            let r = self.git(&["commit", "--quiet", "-m", &msg]).ok()?;
            if !r.status.success() {
                return None;
            }
            let o = self.git(&["rev-parse", "--short", "HEAD"]).ok()?;
            String::from_utf8_lossy(&o.stdout).trim().to_string()
        };
        if SINCE_PRUNE.fetch_add(1, Ordering::SeqCst) + 1 >= PRUNE_EVERY {
            SINCE_PRUNE.store(0, Ordering::SeqCst);
            self.prune(RETENTION);
        }
        let now = now_s() as u64;
        if now.saturating_sub(LAST_SWEEP.load(Ordering::SeqCst)) >= 86_400 {
            LAST_SWEEP.store(now, Ordering::SeqCst);
            self.sweep_stale(STALE_DAYS);
        }
        Some(short)
    }

    /// R151: keep `keep` snapshots and the newest undo tags; reclaim the rest.
    pub fn prune(&self, keep: usize) -> usize {
        let gd = self.gitdir();
        if !gd.join("HEAD").exists() || keep == 0 {
            return 0;
        }
        let Ok(_g) = REPO_LOCK.lock() else { return 0 };
        let out = |args: &[&str]| self.git(args).map(|o| String::from_utf8_lossy(&o.stdout).to_string()).unwrap_or_default();
        let tags = out(&["tag", "--list", "undo-*", "--sort=-creatordate"]);
        for stale in tags.split_whitespace().skip(UNDO_TAG_RETENTION) {
            let _ = self.git(&["tag", "-d", stale]);
        }
        let revs: Vec<String> = out(&["rev-list", "HEAD"]).split_whitespace().map(String::from).collect();
        let mut cut = 0;
        if revs.len() > keep {
            let _ = fs::write(gd.join("shallow"), format!("{}\n", revs[keep - 1]));
            cut = revs.len() - keep;
        }
        let _ = self.git(&["reflog", "expire", "--expire=now", "--all"]);
        let _ = self.git(&["gc", "--prune=now", "--quiet"]);
        cut
    }

    /// R450: remove shadow repos whose folder is gone or that are older than `max_age_days`.
    pub fn sweep_stale(&self, max_age_days: u64) -> usize {
        let base = self.home.join("checkpoints");
        let Ok(rd) = fs::read_dir(&base) else { return 0 };
        let now = now_s();
        let mut removed = 0;
        for e in rd.flatten() {
            let gd = e.path();
            let name = e.file_name().to_string_lossy().to_string();
            let is_repo = name.len() == 16
                && name.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
                && e.file_type().map(|t| t.is_dir() && !t.is_symlink()).unwrap_or(false)
                && gd.join("HEAD").exists();
            if !is_repo {
                continue;
            }
            let folder = fs::read_to_string(gd.join(CWD_MARK)).map(|s| s.trim().to_string()).unwrap_or_default();
            let gone = !folder.is_empty() && !Path::new(&folder).exists();
            let newest = [gd.clone(), gd.join("HEAD"), gd.join("logs").join("HEAD"), gd.join("last-mutation.json"), gd.join(CWD_MARK)]
                .iter()
                .filter_map(|f| fs::metadata(f).and_then(|m| m.modified()).ok())
                .filter_map(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs_f64())
                .fold(0.0, f64::max);
            if gone || now - newest > (max_age_days * 86_400) as f64 {
                let _g = REPO_LOCK.lock();
                if fs::remove_dir_all(&gd).is_ok() {
                    removed += 1;
                }
            }
        }
        removed
    }

    fn last_mutation_path(&self) -> PathBuf {
        self.gitdir().join("last-mutation.json")
    }

    /// R181/R193: the target file's bytes before a write_file/edit_file/apply_patch.
    pub fn snapshot_before_write(&self, path: &str) {
        let marker = self.last_mutation_path();
        let ok = (|| -> Option<()> {
            let p = crate::paths::expand_user(path);
            let abs = if p.is_absolute() { p } else { std::env::current_dir().ok()?.join(p) };
            let target = fs::canonicalize(&abs).unwrap_or_else(|_| normalize(&abs));
            fs::create_dir_all(marker.parent()?).ok()?;
            let existed = target.is_file();
            if existed && fs::metadata(&target).ok()?.len() > MAX_SNAPSHOT_BYTES {
                return None;
            }
            let data = if existed { fs::read(&target).ok()? } else { Vec::new() };
            if data.len() as u64 > MAX_SNAPSHOT_BYTES {
                return None;
            }
            let b64 = if existed { json!(base64::engine::general_purpose::STANDARD.encode(&data)) } else { json!(null) };
            let rec = json!({"path": target.to_string_lossy(), "existed": existed, "content_b64": b64, "at": now_s()});
            crate::paths::write_atomic(&marker, aurora_core::pyjson::dumps_ascii(&rec).as_bytes(), None).ok()
        })();
        if ok.is_none() {
            let _ = fs::remove_file(&marker);
        }
    }

    /// R273: a gated call with no single target invalidates the file snapshot.
    pub fn clear_last_mutation(&self) {
        let _ = fs::remove_file(self.last_mutation_path());
    }

    /// The shadow HEAD's short hash, if any checkpoint exists.
    pub fn head(&self) -> Option<String> {
        if !self.gitdir().join("HEAD").exists() {
            return None;
        }
        let o = self.git(&["rev-parse", "--short", "HEAD"]).ok()?;
        let s = String::from_utf8_lossy(&o.stdout).trim().to_string();
        (!s.is_empty()).then_some(s)
    }
}

/// `Path.resolve()` for a path that does not exist: `..` and `.` folded lexically.
fn normalize(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

fn gitignore_literal(rel: &str) -> String {
    let mut out: String = rel.chars().flat_map(|c| if "\\*?[".contains(c) { vec!['\\', c] } else { vec![c] }).collect();
    if out.ends_with(' ') {
        out.pop();
        out.push_str("\\ ");
    }
    out
}

impl aurora_core::agent::Checkpointer for Rewind {
    fn before_mutation(&self, tool: &str, args: &aurora_core::Args, prompt: &str) {
        self.checkpoint(&format!("[{tool}] {prompt}"));
        match args.get("path") {
            Some(v) if !v.is_null() && v.as_str() != Some("") && v != &json!(false) => {
                let p = v.as_str().map(String::from).unwrap_or_else(|| v.to_string());
                self.snapshot_before_write(&p);
            }
            _ => self.clear_last_mutation(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha1_known_vectors() {
        assert_eq!(sha1_hex(b"abc"), "a9993e364706816aba3e25717850c26c9cd0d89d");
        assert_eq!(sha1_hex(b""), "da39a3ee5e6b4b0d3255bfef95601890afd80709");
        assert_eq!(sha1_hex(&[b'a'; 1000]), "291e9a6c66994949b57ba5e650361e98fc36b1ba");
    }

    #[test]
    fn gitignore_literals() {
        assert_eq!(gitignore_literal("a*b?[c].txt "), "a\\*b\\?\\[c].txt\\ ");
    }
}
