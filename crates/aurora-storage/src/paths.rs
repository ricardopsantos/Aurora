//! AURORA_HOME and atomic writes (Python `aurora/paths.py`).

use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use crate::{Result, StorageError};

fn home_dir() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

/// `~` and `~/…` expanded the way Python's `Path.expanduser` does.
pub fn expand_user(p: &str) -> PathBuf {
    if p == "~" {
        home_dir()
    } else if let Some(rest) = p.strip_prefix("~/") {
        home_dir().join(rest)
    } else {
        PathBuf::from(p)
    }
}

/// $AURORA_HOME → the path in ~/.aurora-path → ~/.aurora. Created when
/// missing; group/other access is removed, never added (R450).
pub fn aurora_home() -> Result<PathBuf> {
    let home = match std::env::var("AURORA_HOME") {
        Ok(v) if !v.is_empty() => expand_user(&v),
        _ => {
            let marker = home_dir().join(".aurora-path");
            match fs::read_to_string(&marker) {
                Ok(s) if marker.is_file() => expand_user(s.trim()),
                _ => home_dir().join(".aurora"),
            }
        }
    };
    ensure_dir(&home, "AURORA_HOME")?;
    if let Ok(meta) = fs::metadata(&home) {
        let mode = meta.permissions().mode() & 0o7777;
        if mode & 0o077 != 0 {
            let _ = fs::set_permissions(&home, fs::Permissions::from_mode(mode & !0o077));
        }
    }
    Ok(home)
}

pub fn sessions_dir() -> Result<PathBuf> {
    let d = aurora_home()?.join("sessions");
    ensure_dir(&d, "the sessions directory")?;
    Ok(d)
}

fn ensure_dir(path: &Path, what: &str) -> Result<()> {
    if path.exists() && !path.is_dir() {
        return Err(StorageError::Io(format!("AURORA_HOME must name a directory: {} ({what})", path.display())));
    }
    fs::create_dir_all(path).map_err(|e| {
        StorageError::Io(format!("cannot create {what}: {}\n{e}. Check AURORA_HOME", path.display()))
    })
}

/// Replace a file in one step or not at all (R146a, R201): a sibling temp
/// file, fsynced, given its mode before it becomes visible, then renamed.
pub fn write_atomic(path: &Path, data: &[u8], mode: Option<u32>) -> Result<()> {
    let dir = path.parent().filter(|d| !d.as_os_str().is_empty()).unwrap_or(Path::new("."));
    fs::create_dir_all(dir)?;
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("file");
    let mut tmp = tempfile::Builder::new().prefix(&format!(".{name}.")).suffix(".tmp").tempfile_in(dir)?;
    tmp.write_all(data)?;
    tmp.as_file().sync_all()?;
    if let Some(m) = mode {
        fs::set_permissions(tmp.path(), fs::Permissions::from_mode(m))?;
    }
    tmp.persist(path).map_err(|e| StorageError::Io(e.error.to_string()))?;
    if let Ok(d) = fs::File::open(dir) {
        let _ = d.sync_all();
    }
    Ok(())
}
