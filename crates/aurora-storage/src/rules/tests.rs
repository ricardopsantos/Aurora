use super::*;
use serde_json::json;

fn a(v: Value) -> Args {
    v.as_object().unwrap().clone()
}
fn r(v: Value) -> Rules {
    v.as_object().unwrap().clone()
}
fn cmd(c: &str) -> Args {
    a(json!({ "command": c }))
}
fn allowed(rules: Value, c: &str) -> bool {
    is_allowed("run_command", &cmd(c), &r(rules), &Rules::new())
}

#[test]
fn prefix_rules_stop_at_token_boundaries_and_chains() {
    let rules = json!({"run_command": ["git status", "ls"]});
    assert!(allowed(rules.clone(), "git status --short"));
    assert!(!allowed(rules.clone(), "git statusx"));
    assert!(!allowed(rules.clone(), "gitk status"));
    assert!(allowed(rules.clone(), "ls -la /tmp"));
    assert!(!allowed(rules.clone(), "ls && rm -rf ~"));
    assert!(!allowed(rules, "ls $(id)"));
}

#[test]
fn dangerous_commands_only_match_exactly() {
    let rules = json!({"run_command": ["rm -rf", "rm", "python3.11 -c", "git -c", "find"]});
    assert!(!allowed(rules.clone(), "rm -rf /"));
    assert!(!allowed(rules.clone(), "rm x"));
    assert!(!allowed(rules.clone(), "python3.11 -c 'print(1)'"));
    assert!(!allowed(rules.clone(), "git -c core.sshCommand=x fetch"));
    assert!(allowed(rules.clone(), "find /a -name x"));
    assert!(!allowed(rules, "find / -delete"));
    assert!(allowed(json!({"run_command": ["rm -rf ./build"]}), "rm -rf ./build"));
}

#[test]
fn only_two_token_rules_are_prefix_rules() {
    let rules = json!({"run_command": ["zip -r a.zip src"]});
    assert!(allowed(rules.clone(), "zip -r a.zip src"));
    assert!(!allowed(rules, "zip -r a.zip src -TT 'cmd'"));
}

#[test]
fn deny_rules_match_loosely_and_chained() {
    let deny = r(json!({"run_command": ["dd", "git push"]}));
    assert!(is_denied("run_command", &cmd("dd if=/dev/zero of=/dev/sda"), &deny));
    assert!(is_denied("run_command", &cmd("git push origin main"), &deny));
    assert!(is_denied("run_command", &cmd("git push && ls"), &deny));
    assert!(!is_denied("run_command", &cmd("git pull"), &deny));
    assert!(!is_denied("run_command", &cmd("dd"), &Rules::new()));
}

#[test]
fn path_rules_resist_traversal_and_symlinks() {
    let d = std::env::temp_dir().join(format!("aurora-rules-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(d.join("proj")).unwrap();
    std::fs::create_dir_all(d.join("secret")).unwrap();
    std::os::unix::fs::symlink(d.join("secret"), d.join("proj/out")).unwrap();
    let p = d.display().to_string();
    let rules = r(json!({"write_file": [format!("{p}/proj/*")]}));
    let w = |path: String| is_allowed("write_file", &a(json!({"path": path, "content": "x"})), &rules, &Rules::new());
    assert!(w(format!("{p}/proj/a.txt")));
    assert!(!w(format!("{p}/proj/../secret/k.txt")));
    assert!(!w(format!("{p}/proj/out/k.txt")), "a symlink inside the folder must not escape it");
    let deny = r(json!({"write_file": [format!("{p}/proj/*")]}));
    assert!(is_denied("write_file", &a(json!({"path": format!("{p}/proj/out/k.txt")})), &deny));
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn rules_stored_by_always_allow_follow_python() {
    let rf = |c: &str| rule_for("run_command", &cmd(c));
    assert_eq!(rf("git push origin main"), "git push");
    assert_eq!(rf("find /a -name x"), "find");
    assert_eq!(rf("rm -rf ./build"), "rm -rf ./build");
    assert_eq!(rf("ls && pwd"), "ls '&&' pwd");
    assert_eq!(rf("sort -o out in"), "sort -o out in");
    assert_eq!(rf("psql -c 'select 1'"), "psql -c 'select 1'");
    assert_eq!(rule_for("write_file", &a(json!({"path": "/a/../b/c"}))), "/b/c");
    assert_eq!(rule_for("mcp_x_y", &a(json!({"title": "t"}))), "*");
    assert_eq!(rule_for("web_fetch", &a(json!({"url": "https://Example.com:8080/x?y"}))), "https://Example.com:8080/*");
    assert_eq!(rule_scope("run_command", &cmd("git push origin")).1, "any command starting `git push`");
    assert_eq!(rule_scope("run_command", &cmd("ls /a")).1, "`ls` with any arguments");
}

#[test]
fn a_command_aimed_elsewhere_and_wait_until_then_always_ask() {
    let rules = json!({"run_command": ["git status"], "wait_until": ["curl x"]});
    let with_cwd = a(json!({"command": "git status", "cwd": "/"}));
    assert!(!is_allowed("run_command", &with_cwd, &r(rules.clone()), &Rules::new()));
    let then = a(json!({"command": "curl x", "then": "echo hi"}));
    assert!(!is_allowed("wait_until", &then, &r(rules), &Rules::new()));
    let deny = r(json!({"run_command": ["echo"]}));
    assert!(is_denied("wait_until", &then, &deny));
}

#[test]
fn version_suffixes() {
    assert_eq!(unversioned("python3.11"), "python");
    assert_eq!(unversioned("pip3"), "pip");
    assert_eq!(unversioned("node20"), "node");
    assert_eq!(unversioned("perl5.36"), "perl");
    assert_eq!(unversioned("git"), "git");
    assert_eq!(unversioned("gcc-12"), "gcc");
}
