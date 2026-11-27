//! R475: the box the model's shell commands run in (Python `aurora/sandbox.py`): bubblewrap on Linux,
//! `sandbox-exec` on macOS (R501).
//! The machine is read-only except the workspace, Aurora's own `workspace/`, `~/.cache`, extra writable
//! folders and a private `/tmp`; secret places and ways out of the box are hidden. Before first use the box is
//! checked by a real trial run; a machine where that fails has no box and the caller decides what that means.

use std::path::{Path, PathBuf};
use std::process::Command;

const HIDDEN_HOME: [&str; 13] = [".ssh", ".gnupg", ".aws", ".docker", ".kube", ".password-store", ".netrc", ".git-credentials",
    ".config/gcloud", ".config/gh", ".azure", ".pgpass", ".my.cnf"];
const HIDDEN_ABS: [&str; 1] = ["/etc/environment"];
const SOCKETS: [&str; 4] = ["/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock", "/run/podman/podman.sock"];
/// Variables that point a command at a way out of the box.
const DROP_ENV: [&str; 11] = ["SSH_AUTH_SOCK", "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "DOCKER_HOST",
    "TMUX", "TMUX_PANE", "STY", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"];

#[derive(Clone, Debug)]
pub struct Policy {
    pub home: PathBuf,
    pub aurora_home: PathBuf,
    pub network: bool,
    pub writable: Vec<String>,
    pub hide: Vec<String>,
}

fn real(p: impl AsRef<Path>) -> PathBuf {
    std::fs::canonicalize(p.as_ref()).unwrap_or_else(|_| p.as_ref().to_path_buf())
}

fn s(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

impl Policy {
    /// The box's own /tmp, kept between commands.
    pub fn tmp_dir(&self) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let d = self.aurora_home.join("sandbox").join("tmp");
        let _ = std::fs::create_dir_all(&d);
        let _ = std::fs::set_permissions(&d, std::fs::Permissions::from_mode(0o700));
        d
    }

    pub fn writable_dirs(&self, workspace: &Path) -> Vec<PathBuf> {
        let mut all = vec![workspace.to_path_buf(), self.aurora_home.join("workspace"), self.home.join(".cache")];
        all.extend(self.writable.iter().map(|w| aurora_storage::pathlike::expanduser(w).into()));
        let home = real(&self.home);
        let mut out: Vec<PathBuf> = Vec::new();
        for d in all {
            let d = real(&d);
            if d.is_dir() && !out.contains(&d) && d != Path::new("/") && d != home {
                out.push(d);
            }
        }
        out
    }

    pub fn hidden(&self) -> Vec<PathBuf> {
        let mut hs: Vec<PathBuf> = HIDDEN_HOME.iter().map(|h| self.home.join(h)).collect();
        hs.extend(HIDDEN_ABS.iter().map(PathBuf::from));
        hs.extend(self.hide.iter().map(|h| PathBuf::from(aurora_storage::pathlike::expanduser(h))));
        hs.into_iter().filter(|h| h.symlink_metadata().is_ok()).map(real).collect()
    }

    fn empty_file(&self) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let f = self.aurora_home.join("sandbox").join("empty");
        if !f.exists() {
            let _ = std::fs::create_dir_all(f.parent().expect("has parent"));
            let _ = std::fs::write(&f, "");
            let _ = std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o400));
        }
        f
    }
}

/// Python `_bwrap_args`. Order matters: a later mount covers an earlier one.
fn bwrap_args(p: &Policy, workspace: &Path, chdir: &Path) -> Vec<String> {
    let mut a: Vec<String> = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--unshare-cgroup-try", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"].map(String::from).to_vec();
    let mut push = |xs: &[&str]| a.extend(xs.iter().map(|x| x.to_string()));
    if !p.network {
        push(&["--unshare-net"]);
    }
    push(&["--bind", &s(&p.tmp_dir()), "/tmp"]);
    let home = real(&p.home);
    if home == Path::new("/tmp") || home.starts_with("/tmp/") {
        push(&["--ro-bind", &s(&home), &s(&home)]);
    }
    let rw = p.writable_dirs(workspace);
    for d in &rw {
        push(&["--bind", &s(d), &s(d)]);
    }
    // SAFETY: getuid cannot fail.
    let run_user = format!("/run/user/{}", unsafe { libc::getuid() });
    if Path::new(&run_user).is_dir() {
        push(&["--tmpfs", &run_user]);
    }
    for sock in SOCKETS {
        let writable = std::ffi::CString::new(sock).is_ok_and(|c| unsafe { libc::access(c.as_ptr(), libc::W_OK) } == 0);
        if Path::new(sock).symlink_metadata().is_ok() && writable {
            push(&["--ro-bind", "/dev/null", sock]);
        }
    }
    let empty = p.empty_file();
    for h in p.hidden() {
        if h.is_dir() { push(&["--tmpfs", &s(&h)]) } else { push(&["--ro-bind", &s(&empty), &s(&h)]) }
    }
    let ah = real(&p.aurora_home);
    if ah.is_dir() {
        push(&["--tmpfs", &s(&ah)]);
        let (sk, wk) = (ah.join("skills"), ah.join("workspace"));
        if sk.is_dir() {
            push(&["--ro-bind", &s(&sk), &s(&sk)]);
        }
        if wk.is_dir() {
            push(&["--bind", &s(&wk), &s(&wk)]);
        }
    }
    for d in &rw {
        for g in [".git/hooks", ".git/config"] {
            let f = d.join(g);
            if f.exists() {
                push(&["--ro-bind", &s(&f), &s(&f)]);
            }
        }
    }
    push(&["--setenv", "TMPDIR", "/tmp", "--chdir", &s(chdir)]);
    a
}

fn on_path(name: &str) -> bool {
    std::env::var_os("PATH").is_some_and(|p| std::env::split_paths(&p).any(|d| d.join(name).is_file()))
}

/// Python `platform_tool()`: the box this machine has, if any.
pub fn platform_tool() -> Option<&'static str> {
    if cfg!(target_os = "linux") && on_path("bwrap") {
        Some("bwrap")
    } else if cfg!(target_os = "macos") && on_path("sandbox-exec") {
        Some("sandbox-exec")
    } else {
        None
    }
}

pub fn available() -> bool {
    platform_tool().is_some()
}

fn sb_str(s: &str) -> String {
    format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""))
}

/// Python `macos_profile`: the sandbox-exec profile. In SBPL the last matching rule wins, so: allow everything,
/// deny writes and the hidden places, then allow the writable folders back. `darwin_tmp` is the per-user temp dir.
pub fn macos_profile(p: &Policy, workspace: &Path, darwin_tmp: &Path) -> String {
    let tmp = s(&real(p.tmp_dir()));
    let ah = real(&p.aurora_home);
    let mut w: Vec<String> = p.writable_dirs(workspace).iter().map(|d| s(&real(d))).collect();
    w.push(tmp);
    let subpaths = |xs: &[String]| xs.iter().map(|d| format!("(subpath {})", sb_str(d))).collect::<Vec<_>>().join(" ");
    let mut wt = w.clone();
    wt.push(s(&real(darwin_tmp)));
    let mut lines = vec![
        "(version 1)".to_string(),
        "(allow default)".to_string(),
        "(deny file-write* (subpath \"/\"))".to_string(),
        format!("(allow file-write* {} (literal \"/dev/null\") (literal \"/dev/zero\") (literal \"/dev/dtracehelper\") (regex #\"^/dev/tty\") (regex #\"^/dev/fd/\"))", subpaths(&wt)),
    ];
    let mut hidden: Vec<String> = p.hidden().iter().map(|h| s(h)).collect();
    hidden.push(s(&ah));
    lines.push(format!("(deny file-read* file-write* {})", subpaths(&hidden)));
    let back_ro = vec![s(&ah.join("skills"))];
    let back_rw = vec![s(&ah.join("workspace"))];
    lines.push(format!("(allow file-read* {})", subpaths(&[back_ro, back_rw.clone()].concat())));
    lines.push(format!("(allow file-write* {})", subpaths(&back_rw)));
    let protect: Vec<String> = w.iter().flat_map(|d| [".git/hooks", ".git/config"].map(|g| s(&Path::new(d).join(g)))).collect();
    lines.push(format!("(deny file-write* {})", subpaths(&protect)));
    let mut socks: Vec<String> = SOCKETS.iter().map(|x| x.to_string()).collect();
    socks.push(s(&p.home.join(".docker/run/docker.sock")));
    lines.push(format!(
        "(deny network-outbound {})",
        socks.iter().map(|x| format!("(remote unix-socket (path-literal {}))", sb_str(x))).collect::<Vec<_>>().join(" ")
    ));
    if !p.network {
        lines.push("(deny network-outbound (remote ip))".into());
    }
    lines.join("\n") + "\n"
}

/// The argv that runs `command` inside the box. `workspace` is what it may write (never the command's own cwd).
/// With sandbox-exec the caller's working directory sets where it starts.
pub fn argv(p: &Policy, command: &str, workspace: &Path, shell: &str, chdir: Option<&Path>) -> Vec<String> {
    let mut a = if platform_tool() == Some("sandbox-exec") {
        vec!["sandbox-exec".into(), "-p".into(), macos_profile(p, workspace, &std::env::temp_dir())]
    } else {
        bwrap_args(p, workspace, chdir.unwrap_or(workspace))
    };
    a.extend([shell.to_string(), "-c".into(), command.to_string()]);
    a
}

/// The environment a boxed command gets: no agent or bus variables, TMPDIR in the private /tmp.
pub fn env(base: impl Iterator<Item = (String, String)>) -> Vec<(String, String)> {
    let base: Vec<(String, String)> = base.collect();
    // bwrap gives the box its own /tmp; sandbox-exec keeps the per-user temp dir (Python `env`)
    let tmpdir = if platform_tool() == Some("bwrap") {
        "/tmp".to_string()
    } else {
        base.iter().find(|(k, _)| k == "TMPDIR").map(|(_, v)| v.clone()).unwrap_or_else(|| "/tmp".into())
    };
    let mut e: Vec<(String, String)> = base.into_iter().filter(|(k, _)| !DROP_ENV.contains(&k.as_str()) && k != "TMPDIR").collect();
    e.push(("TMPDIR".into(), tmpdir));
    e.push(("AURORA_SANDBOX".into(), "1".into()));
    e
}

const PROBE: &str = r#"
set -u
ws=$1; hid=$2
echo probe > "$ws/.aurora-sandbox-probe" && rm -f "$ws/.aurora-sandbox-probe" || { echo "workspace not writable"; exit 1; }
if ( echo x > "$HOME/.aurora-sandbox-escape" ) 2>/dev/null; then rm -f "$HOME/.aurora-sandbox-escape"; echo "home writable"; exit 1; fi
if ls "$hid" >/dev/null 2>&1 && [ -n "$(ls -A "$hid" 2>/dev/null)" ]; then echo "hidden folder readable"; exit 1; fi
echo ok
"#;

/// A real trial run of the box: `Ok(())` or why this machine cannot have one.
pub fn check(p: &Policy) -> Result<(), String> {
    let Some(tool) = platform_tool() else {
        return Err(if cfg!(target_os = "linux") { "bubblewrap is not installed (apt install bubblewrap)".into() } else { "no sandbox tool on this system".into() });
    };
    let ws = tempfile::tempdir().map_err(|e| format!("{tool} trial run failed: {e}"))?;
    let hid = tempfile::tempdir().map_err(|e| format!("{tool} trial run failed: {e}"))?;
    std::fs::write(hid.path().join("secret"), "x").map_err(|e| e.to_string())?;
    let mut q = p.clone();
    q.writable = vec![];
    q.hide.push(s(hid.path()));
    let probe = format!("sh -c {} probe {} {}", aurora_storage::pathlike::shlex_quote(PROBE), aurora_storage::pathlike::shlex_quote(&s(ws.path())),
        aurora_storage::pathlike::shlex_quote(&s(hid.path())));
    let a = argv(&q, &probe, ws.path(), "/bin/sh", None);
    let out = Command::new(&a[0]).args(&a[1..]).env_clear().envs(env(std::env::vars())).current_dir(ws.path())
        .stdin(std::process::Stdio::null()).output()
        .map_err(|e| format!("{tool} trial run failed: {e}"))?;
    let text = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
    let text = text.trim();
    if out.status.success() && text.ends_with("ok") {
        return Ok(());
    }
    let tail: String = text.chars().rev().take(200).collect::<Vec<_>>().into_iter().rev().collect();
    Err(format!("{tool} trial run failed: {}", if tail.is_empty() { format!("exit {}", out.status.code().unwrap_or(-1)) } else { tail }))
}

/// `runtime:` settings to a policy (Python `policy_from`).
pub fn policy_from(runtime: &serde_json::Value, home: &Path, aurora_home: &Path) -> Policy {
    let strs = |k: &str| -> Vec<String> {
        runtime[k].as_array().map(|a| a.iter().map(|v| v.as_str().map(String::from).unwrap_or_else(|| v.to_string())).collect()).unwrap_or_default()
    };
    Policy { home: home.to_path_buf(), aurora_home: aurora_home.to_path_buf(), network: runtime["sandbox_network"].as_bool() != Some(false),
        writable: strs("sandbox_writable"), hide: strs("sandbox_hide") }
}

/// `runtime.sandbox`: "web" (default), "all" or "off"; anything else is "web".
pub fn mode_from(runtime: &serde_json::Value) -> &'static str {
    match runtime["sandbox"].as_str().unwrap_or("web").trim().to_lowercase().as_str() {
        "all" => "all",
        "off" => "off",
        _ => "web",
    }
}


#[cfg(test)]
mod tests {
    use super::*;
    use crate::command::RunCommand;
    use aurora_core::{CancellationToken, Tool};
    use serde_json::json;

    struct World {
        _t: tempfile::TempDir,
        home: PathBuf,
        ws: PathBuf,
        policy: Policy,
    }

    fn world() -> Option<World> {
        // macOS: the per-user temp dir is writable in the box (Python's profile), so the test home lives elsewhere
        let t = if cfg!(target_os = "macos") {
            tempfile::tempdir_in(std::env::var("HOME").unwrap()).unwrap()
        } else {
            tempfile::tempdir().unwrap()
        };
        let root = real(t.path());
        let (home, ah, ws) = (root.join("home"), root.join("home/.aurora"), root.join("ws"));
        for d in [home.join(".ssh"), home.join(".cache"), ah.join("workspace"), ah.join("skills"), ws.join(".git/hooks")] {
            std::fs::create_dir_all(d).unwrap();
        }
        std::fs::write(home.join(".ssh/id_ed25519"), "PRIVATE").unwrap();
        std::fs::write(ah.join("config.yaml"), "api: x").unwrap();
        std::fs::write(ws.join(".git/config"), "[core]").unwrap();
        let policy = Policy { home: home.clone(), aurora_home: ah, network: true, writable: vec![], hide: vec![] };
        match check(&policy) {
            Ok(()) => Some(World { _t: t, home, ws, policy }),
            Err(why) => {
                eprintln!("no sandbox here: {why}");
                None
            }
        }
    }

    fn sh(w: &World, cmd: &str) -> (bool, String) {
        let a = argv(&w.policy, cmd, &w.ws, "/bin/sh", None);
        let o = Command::new(&a[0]).args(&a[1..]).current_dir(&w.ws).env_clear().envs(env(std::env::vars()))
            .env("HOME", &w.home).stdin(std::process::Stdio::null()).output().unwrap();
        (o.status.success(), format!("{}{}", String::from_utf8_lossy(&o.stdout), String::from_utf8_lossy(&o.stderr)))
    }

    #[test]
    fn the_box_lets_the_workspace_work_and_hides_secrets() {
        let Some(w) = world() else { return };
        assert!(sh(&w, "echo hi > a.txt && cat a.txt").0);
        assert!(w.ws.join("a.txt").exists());
        assert!(!sh(&w, "cat ~/.ssh/id_ed25519").0, "secret folder readable");
        assert!(!sh(&w, "echo x > ~/escape").0, "home writable");
        assert!(!sh(&w, "cat ~/.aurora/config.yaml").0, "aurora home readable");
        assert!(!sh(&w, "echo x >> .git/config").0, ".git/config writable");
        assert!(sh(&w, "echo $AURORA_SANDBOX").1.trim() == "1");
        if platform_tool() == Some("bwrap") {
            assert!(sh(&w, "echo x > /tmp/mine && cat /tmp/mine").0);
            assert!(!Path::new("/tmp/mine").exists(), "the private /tmp is not the host's");
        }
    }

    #[test]
    fn a_boxed_run_command_cannot_write_outside_and_the_cwd_argument_does_not_open_a_folder() {
        let Some(w) = world() else { return };
        // macOS: the per-user temp dir is writable in the box, so "outside" must be elsewhere (as in world())
        let other = if cfg!(target_os = "macos") { tempfile::tempdir_in(std::env::var("HOME").unwrap()).unwrap() } else { tempfile::tempdir().unwrap() };
        let tool = RunCommand { sandbox: Some(w.policy.clone()), ..Default::default() };
        let args = json!({"command": "echo x > f.txt", "cwd": other.path().to_str().unwrap()}).as_object().unwrap().clone();
        let out = futures_block(tool.run(&args, &CancellationToken::new()));
        assert!(out.contains("[exit 1]"), "{out}");
        assert!(!other.path().join("f.txt").exists());
    }

    fn futures_block<F: std::future::Future>(f: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(f)
    }

    #[test]
    fn the_argv_keeps_the_order_that_makes_hiding_win() {
        let t = tempfile::tempdir().unwrap();
        let root = real(t.path());
        std::fs::create_dir_all(root.join("home/.ssh")).unwrap();
        std::fs::create_dir_all(root.join("ws")).unwrap();
        let p = Policy { home: root.join("home"), aurora_home: root.join("home/.aurora"), network: false, writable: vec![s(&root.join("home"))], hide: vec![] };
        let mut a = bwrap_args(&p, &root.join("ws"), &root.join("ws"));
        a.extend(["/bin/sh".into(), "-c".into(), "true".into()]);
        let at = |needle: &str| a.iter().position(|x| x == needle).unwrap();
        assert!(at("--unshare-net") < at("--chdir"));
        assert!(a.windows(2).any(|w| w[0] == "--tmpfs" && w[1] == s(&root.join("home/.ssh"))));
        let bind_ws = a.windows(3).position(|w| w[0] == "--bind" && w[1] == s(&root.join("ws"))).unwrap();
        let hide = a.windows(2).position(|w| w[0] == "--tmpfs" && w[1] == s(&root.join("home/.ssh"))).unwrap();
        assert!(bind_ws < hide, "writable folders come before what is hidden");
        assert_eq!(&a[a.len() - 3..], ["/bin/sh", "-c", "true"]);
    }

    #[test]
    fn settings_are_read_like_python() {
        let rt = json!({"sandbox": " ALL ", "sandbox_network": false, "sandbox_writable": ["~/r"], "sandbox_hide": []});
        assert_eq!(mode_from(&rt), "all");
        assert_eq!(mode_from(&json!({"sandbox": "nonsense"})), "web");
        assert_eq!(mode_from(&json!({})), "web");
        assert!(!policy_from(&rt, Path::new("/h"), Path::new("/h/.aurora")).network);
        let e: Vec<String> = env([("SSH_AUTH_SOCK".to_string(), "x".to_string()), ("A".to_string(), "1".to_string())].into_iter()).into_iter().map(|(k, _)| k).collect();
        assert_eq!(e, ["A", "TMPDIR", "AURORA_SANDBOX"]);
    }
}
