//! Python's `shlex.split` / `shlex.quote` / `fnmatch` / `os.path` helpers, byte
//! for byte where the approval matcher depends on them (R7, R95g, R195, R209).

use std::path::{Path, PathBuf};

/// `shlex.split(s)` (POSIX mode, no comments). `Err` where Python raises
/// ValueError (unbalanced quote, trailing backslash).
/// The error is Python's ValueError: there is nothing more to say about it.
#[allow(clippy::result_unit_err)]
pub fn shlex_split(s: &str) -> Result<Vec<String>, ()> {
    #[derive(Clone, Copy, PartialEq)]
    enum St {
        Space,
        Word,
        Quote(char),
        Esc(Option<char>), // the state to return to: None = outside quotes (word)
    }
    let mut out = Vec::new();
    let (mut tok, mut quoted) = (String::new(), false);
    let mut st = St::Space;
    for c in s.chars() {
        let ws = matches!(c, ' ' | '\t' | '\r' | '\n');
        st = match st {
            St::Space => match c {
                _ if ws => {
                    if !tok.is_empty() || quoted {
                        out.push(std::mem::take(&mut tok));
                        quoted = false;
                    }
                    St::Space
                }
                '\\' => St::Esc(None),
                '\'' | '"' => St::Quote(c),
                _ => {
                    tok.push(c);
                    St::Word
                }
            },
            St::Word => match c {
                _ if ws => {
                    if !tok.is_empty() || quoted {
                        out.push(std::mem::take(&mut tok));
                        quoted = false;
                    }
                    St::Space
                }
                '\'' | '"' => St::Quote(c),
                '\\' => St::Esc(None),
                _ => {
                    tok.push(c);
                    St::Word
                }
            },
            St::Quote(q) => {
                if c == q {
                    quoted = true;
                    St::Word
                } else if c == '\\' && q == '"' {
                    St::Esc(Some(q))
                } else {
                    tok.push(c);
                    St::Quote(q)
                }
            }
            St::Esc(back) => match back {
                None => {
                    tok.push(c);
                    St::Word
                }
                Some(q) => {
                    if c != q && c != '\\' {
                        tok.push('\\');
                    }
                    tok.push(c);
                    St::Quote(q)
                }
            },
        };
    }
    match st {
        St::Quote(_) | St::Esc(_) => return Err(()),
        _ => {}
    }
    if !tok.is_empty() || quoted {
        out.push(tok);
    }
    Ok(out)
}

/// `shlex.quote`.
pub fn shlex_quote(s: &str) -> String {
    if s.is_empty() {
        return "''".into();
    }
    if s.chars().all(|c| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c)) {
        return s.to_string();
    }
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}

pub fn shlex_join(toks: &[String]) -> String {
    toks.iter().map(|t| shlex_quote(t)).collect::<Vec<_>>().join(" ")
}

fn passwd_home(user: &str) -> Option<String> {
    let text = std::fs::read_to_string("/etc/passwd").ok()?;
    text.lines().find_map(|l| {
        let f: Vec<&str> = l.split(':').collect();
        (f.len() >= 6 && f[0] == user).then(|| f[5].to_string())
    })
}

/// `os.path.expanduser`.
pub fn expanduser(path: &str) -> String {
    if !path.starts_with('~') {
        return path.to_string();
    }
    let end = path.find('/').unwrap_or(path.len());
    let home = if end == 1 {
        match std::env::var("HOME") {
            Ok(h) => h,
            Err(_) => match passwd_home(&std::env::var("USER").unwrap_or_default()) {
                Some(h) => h,
                None => return path.to_string(),
            },
        }
    } else {
        match passwd_home(&path[1..end]) {
            Some(h) => h,
            None => return path.to_string(),
        }
    };
    let home = home.trim_end_matches('/');
    let out = format!("{home}{}", &path[end..]);
    if out.is_empty() { "/".into() } else { out }
}

/// `posixpath.normpath`.
pub fn normpath(path: &str) -> String {
    let initial = if path.starts_with("//") && !path.starts_with("///") {
        2
    } else {
        usize::from(path.starts_with('/'))
    };
    let mut new: Vec<&str> = Vec::new();
    for comp in path.split('/') {
        if comp.is_empty() || comp == "." {
            continue;
        }
        if comp != ".." || (initial == 0 && new.is_empty()) || new.last() == Some(&"..") {
            new.push(comp);
        } else if !new.is_empty() {
            new.pop();
        }
    }
    let joined = format!("{}{}", "/".repeat(initial), new.join("/"));
    if joined.is_empty() { ".".into() } else { joined }
}

/// `Path(p).expanduser().resolve()` (non-strict: a missing tail is kept).
pub fn resolve(path: &str) -> String {
    let expanded = expanduser(path);
    let mut pending: Vec<String> = Vec::new();
    let mut result = PathBuf::from("/");
    let abs = if expanded.starts_with('/') {
        expanded
    } else {
        format!("{}/{expanded}", std::env::current_dir().map(|d| d.display().to_string()).unwrap_or_default())
    };
    pending.extend(abs.split('/').rev().map(String::from));
    let mut links = 0;
    while let Some(comp) = pending.pop() {
        match comp.as_str() {
            "" | "." => continue,
            ".." => {
                result.pop();
                continue;
            }
            _ => {}
        }
        let next = result.join(&comp);
        match std::fs::symlink_metadata(&next) {
            Ok(m) if m.file_type().is_symlink() => {
                links += 1;
                if links > 40 {
                    return next.display().to_string(); // a loop: Python keeps the path as is
                }
                let Ok(target) = std::fs::read_link(&next) else { return next.display().to_string() };
                let t = target.display().to_string();
                if t.starts_with('/') {
                    result = PathBuf::from("/");
                }
                pending.extend(t.split('/').rev().map(String::from));
            }
            _ => result = next,
        }
    }
    result.display().to_string()
}

#[derive(Debug)]
enum Piece {
    Star,
    Any,
    Lit(char),
    Set { negate: bool, items: Vec<(char, char)> },
}

fn compile(pat: &str) -> Vec<Piece> {
    let p: Vec<char> = pat.chars().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < p.len() {
        let c = p[i];
        i += 1;
        match c {
            '*' => {
                if !matches!(out.last(), Some(Piece::Star)) {
                    out.push(Piece::Star)
                }
            }
            '?' => out.push(Piece::Any),
            '[' => {
                let mut j = i;
                if j < p.len() && p[j] == '!' {
                    j += 1;
                }
                if j < p.len() && p[j] == ']' {
                    j += 1;
                }
                while j < p.len() && p[j] != ']' {
                    j += 1;
                }
                if j >= p.len() {
                    out.push(Piece::Lit('['));
                } else {
                    let mut body: Vec<char> = p[i..j].to_vec();
                    let negate = body.first() == Some(&'!');
                    if negate {
                        body.remove(0);
                    }
                    let mut items = Vec::new();
                    let mut k = 0;
                    while k < body.len() {
                        if k + 2 < body.len() && body[k + 1] == '-' {
                            items.push((body[k], body[k + 2]));
                            k += 3;
                        } else {
                            items.push((body[k], body[k]));
                            k += 1;
                        }
                    }
                    out.push(Piece::Set { negate, items });
                    i = j + 1;
                }
            }
            _ => out.push(Piece::Lit(c)),
        }
    }
    out
}

/// `fnmatch.fnmatch` on POSIX (case-sensitive; `*` crosses `/`).
pub fn fnmatch(name: &str, pat: &str) -> bool {
    let pieces = compile(pat);
    let s: Vec<char> = name.chars().collect();
    let one = |p: &Piece, c: char| match p {
        Piece::Any => true,
        Piece::Lit(l) => *l == c,
        Piece::Set { negate, items } => items.iter().any(|(a, b)| *a <= c && c <= *b) != *negate,
        Piece::Star => false,
    };
    let (mut pi, mut si) = (0, 0);
    let (mut star, mut mark) = (None, 0);
    while si < s.len() {
        if pi < pieces.len() && matches!(pieces[pi], Piece::Star) {
            star = Some(pi);
            mark = si;
            pi += 1;
        } else if pi < pieces.len() && one(&pieces[pi], s[si]) {
            pi += 1;
            si += 1;
        } else if let Some(sp) = star {
            pi = sp + 1;
            mark += 1;
            si = mark;
        } else {
            return false;
        }
    }
    pieces[pi..].iter().all(|p| matches!(p, Piece::Star))
}

pub fn is_dir(p: &str) -> bool {
    Path::new(p).is_dir()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shlex_matches_python() {
        let sp = |s| shlex_split(s).unwrap();
        assert_eq!(sp("git  status 'a b' \"c d\" e\\ f"), ["git", "status", "a b", "c d", "e f"]);
        assert_eq!(sp("echo ''"), ["echo", ""]);
        assert_eq!(sp(r#"echo "a\"b" "a\nb""#), ["echo", "a\"b", "a\\nb"]);
        assert_eq!(sp("a&&b | c"), ["a&&b", "|", "c"]);
        assert!(shlex_split("echo 'x").is_err() && shlex_split("echo x\\").is_err());
        assert_eq!(shlex_quote("a b"), "'a b'");
        assert_eq!(shlex_quote("it's"), "'it'\"'\"'s'");
        assert_eq!(shlex_quote("a-b_c/d.e"), "a-b_c/d.e");
    }

    #[test]
    fn normpath_and_fnmatch_match_python() {
        assert_eq!(normpath("/a/b/../../c//d/."), "/c/d");
        assert_eq!(normpath("//a"), "//a");
        assert_eq!(normpath("///a"), "/a");
        assert_eq!(normpath("../a/.."), "..");
        assert_eq!(normpath(""), ".");
        assert!(fnmatch("/p/x/../y", "/p/*"));
        assert!(fnmatch("a.py", "*.py") && fnmatch("a/b.py", "*.py") && !fnmatch("a.pyc", "*.py"));
        assert!(fnmatch("abc", "a?c") && fnmatch("b", "[a-c]") && !fnmatch("d", "[a-c]") && fnmatch("d", "[!a-c]"));
        assert!(fnmatch("]", "[]]") && fnmatch("[x", "[x") && fnmatch("", "*"));
    }

    #[test]
    fn resolve_follows_links_and_keeps_missing_tails() {
        let d = tempfile_dir();
        let real = d.join("real");
        std::fs::create_dir(&real).unwrap();
        std::os::unix::fs::symlink(&real, d.join("link")).unwrap();
        let want = format!("{}/real/new/file", std::fs::canonicalize(&d).unwrap().display());
        assert_eq!(resolve(&format!("{}/link/new/file", d.display())), want);
    }

    fn tempfile_dir() -> PathBuf {
        let d = std::env::temp_dir().join(format!("aurora-pl-{}-{}", std::process::id(), std::thread::current().name().unwrap_or("t").replace("::", "_")));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }
}
