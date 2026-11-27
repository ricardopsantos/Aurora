//! Secret detection and redaction (Python `aurora/secrets.py`, R58). Pure
//! text in, matches or text out. Two passes: known key and token shapes
//! (regex), then an entropy fallback for ad-hoc tokens on spans the first
//! pass left alone. Offsets here are byte offsets (Python's are characters);
//! kinds, matched text and redacted output are identical.

use std::collections::HashSet;
use std::sync::LazyLock;

use fancy_regex::Regex;
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Match {
    pub kind: &'static str,
    pub start: usize,
    pub end: usize,
    pub text: String,
}

fn re(p: &str) -> Regex {
    Regex::new(p).unwrap_or_else(|e| panic!("bad pattern {p}: {e}"))
}

struct Pattern {
    name: &'static str,
    re: Regex,
    /// A cheap literal prerequisite (R96g): none of these in the text, no match possible.
    guard: Option<&'static [&'static str]>,
}

static PATTERNS: LazyLock<Vec<Pattern>> = LazyLock::new(|| {
    let p = |name, pat: &str, guard| Pattern { name, re: re(pat), guard };
    vec![
        p("GUID/UUID", r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", None),
        p("AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", Some(&["AKIA", "ASIA"])),
        p("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", Some(&["ghp_", "gho_", "ghu_", "ghs_", "ghr_"])),
        p("Slack token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", Some(&["xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"])),
        p("Stripe key", r"\b(?:sk|pk)_live_[A-Za-z0-9]{16,}\b", Some(&["_live_"])),
        p("OpenAI-style key", r"\bsk-[A-Za-z0-9]{20,}\b", Some(&["sk-"])),
        p("OpenRouter key", r"\bsk-or-v1-[0-9a-fA-F]{32,}\b", Some(&["sk-or-v1-"])),
        p("Bearer token", r"\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*", Some(&["Bearer"])),
        p("Private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----", Some(&["-----BEGIN"])),
        p(
            "Env credential",
            concat!(
                r"(?im)^[ \t]*(?:export[ \t]+)?(?:",
                r"[A-Za-z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Za-z0-9_]*",
                r"|[A-Za-z0-9_]+PWD[A-Za-z0-9_]*",
                r"|(?-i:[A-Z0-9_]+PASS|[a-z0-9_]+_pass)(?=[ \t]*=)",
                r")[ \t]*=[ \t]*\S+"
            ),
            None,
        ),
    ]
});

static CODE_VALUE: LazyLock<Regex> = LazyLock::new(|| re(r#"^(?:["']{2}|[fFrRbB]["']|None$|True$|False$|null$|\w[\w.]*\()"#));
static WORD_SEG: LazyLock<Regex> = LazyLock::new(|| re(r"^(?:[a-z]{1,12}|[A-Z][a-z]{0,11}|[A-Z]{1,5}|\d{1,3}[a-z]{0,2})$"));
static CANDIDATE: LazyLock<Regex> = LazyLock::new(|| re(r"\b[A-Za-z0-9_\-+]{20,}\b"));
static HEX: LazyLock<Regex> = LazyLock::new(|| re(r"^[0-9a-fA-F]+$"));
static DATE: LazyLock<Regex> =
    LazyLock::new(|| re(r"^(?:19|20)\d{2}[-_]?(?:0[1-9]|1[0-2])[-_]?(?:0[1-9]|[12]\d|3[01])(?:_\d{6})?"));

const ENTROPY_THRESHOLD: f64 = 3.6;

/// R328: the value half of an `Env credential` hit is source code, not a secret.
fn is_code_value(line: &str) -> bool {
    let val = line.split_once('=').map_or("", |(_, v)| v).trim();
    CODE_VALUE.is_match(val).unwrap_or(false)
}

/// R328: three or more short ordinary words joined by `-` or `_` is a name.
fn is_word_identifier(token: &str) -> bool {
    let segs: Vec<&str> = token.split(['-', '_']).collect();
    segs.len() >= 3 && segs.iter().all(|s| WORD_SEG.is_match(s).unwrap_or(false))
}

/// SHA-256 of a matched value: what the allowlist stores, never the value.
pub fn hash_value(value: &str) -> String {
    let d = Sha256::digest(value.as_bytes());
    d.iter().map(|b| format!("{b:02x}")).collect()
}

fn shannon_entropy(s: &str) -> f64 {
    let n = s.chars().count() as f64;
    if n == 0.0 {
        return 0.0;
    }
    // counts in first-seen order, like Python's Counter, so the sum is the same
    let mut counts: Vec<(char, usize)> = Vec::new();
    for c in s.chars() {
        match counts.iter_mut().find(|(k, _)| *k == c) {
            Some(e) => e.1 += 1,
            None => counts.push((c, 1)),
        }
    }
    -counts.iter().map(|(_, c)| (*c as f64 / n) * (*c as f64 / n).log2()).sum::<f64>()
}

/// Every match across all patterns plus the entropy fallback, in document
/// order; overlaps keep only the first claim. `allowlist` holds `hash_value`s
/// of confirmed false positives.
pub fn scan(text: &str, allowlist: &HashSet<String>) -> Vec<Match> {
    if text.is_empty() {
        return Vec::new();
    }
    let mut found: Vec<Match> = Vec::new();
    let mut claimed = vec![false; text.len()];
    let overlaps = |c: &[bool], s: usize, e: usize| c[s..e].iter().any(|b| *b);

    for pat in PATTERNS.iter() {
        if let Some(g) = pat.guard {
            if !g.iter().any(|l| text.contains(l)) {
                continue;
            }
        }
        // R450 (S2): /proc/<pid>/environ separates entries with NUL; same length, so offsets hold
        let hay = if pat.name == "Env credential" && text.contains('\0') { text.replace('\0', "\n") } else { text.to_string() };
        for m in pat.re.find_iter(&hay).flatten() {
            let (s, e) = (m.start(), m.end());
            if overlaps(&claimed, s, e) {
                continue;
            }
            if pat.name == "Env credential" && is_code_value(m.as_str()) {
                continue;
            }
            claimed[s..e].fill(true);
            found.push(Match { kind: pat.name, start: s, end: e, text: text[s..e].to_string() });
        }
    }

    for m in CANDIDATE.find_iter(text).flatten() {
        let (s, e) = (m.start(), m.end());
        let spans: Vec<(usize, usize)> = if !overlaps(&claimed, s, e) {
            vec![(s, e)]
        } else {
            // R170b: judge each still-unclaimed run on its own
            let mut v = Vec::new();
            let mut i = s;
            while i < e {
                if claimed[i] {
                    i += 1;
                    continue;
                }
                let mut j = i;
                while j < e && !claimed[j] {
                    j += 1;
                }
                v.push((i, j));
                i = j;
            }
            v
        };
        for (cs, ce) in spans {
            let token = &text[cs..ce];
            if token.is_empty() || HEX.is_match(&token.replace('-', "")).unwrap_or(false) {
                continue;
            }
            if DATE.is_match(token).unwrap_or(false) || is_word_identifier(token) {
                continue;
            }
            let has = |f: fn(&char) -> bool| token.chars().any(|c| f(&c));
            if !(has(char::is_ascii_digit) && has(char::is_ascii_lowercase) && has(char::is_ascii_uppercase)) {
                continue;
            }
            if shannon_entropy(token) < ENTROPY_THRESHOLD {
                continue;
            }
            claimed[cs..ce].fill(true);
            found.push(Match { kind: "High-entropy token", start: cs, end: ce, text: token.to_string() });
        }
    }

    found.sort_by_key(|m| m.start);
    if !allowlist.is_empty() {
        found.retain(|m| !allowlist.contains(&hash_value(&m.text)));
    }
    found
}

/// Replace every matched span with `<secret>` (R96d, R221: nested or
/// overlapping matches never leave the tail of the outer secret behind).
pub fn redact(text: &str, matches: &[Match]) -> String {
    if matches.is_empty() {
        return text.to_string();
    }
    let mut sorted: Vec<&Match> = matches.iter().collect();
    sorted.sort_by_key(|m| m.start);
    let (mut out, mut pos) = (String::new(), 0);
    for m in sorted {
        if m.end <= pos {
            continue;
        }
        let start = m.start.max(pos);
        out.push_str(&text[pos..start]);
        out.push_str("<secret>");
        pos = m.end;
    }
    out.push_str(&text[pos..]);
    out
}

/// Kinds found, never the secret text (the whole point is not to echo it).
pub fn preview(matches: &[Match], max_items: usize) -> String {
    let mut counts: Vec<(&str, usize)> = Vec::new();
    for m in matches {
        match counts.iter_mut().find(|(k, _)| *k == m.kind) {
            Some(e) => e.1 += 1,
            None => counts.push((m.kind, 1)),
        }
    }
    let parts: Vec<String> = counts.iter().map(|(k, v)| if *v > 1 { format!("{v}× {k}") } else { (*k).to_string() }).collect();
    let shown = &parts[..parts.len().min(max_items)];
    let more = parts.len() - shown.len();
    let tail = if more > 0 { format!(" (+{more} more kind{})", if more == 1 { "" } else { "s" }) } else { String::new() };
    shown.join(", ") + &tail
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kinds(t: &str) -> Vec<(&'static str, String)> {
        scan(t, &HashSet::new()).into_iter().map(|m| (m.kind, m.text)).collect()
    }

    #[test]
    fn known_shapes_and_the_env_line() {
        // fake keys built from pieces, so no token-shaped literal sits in the source
        let text = format!("token {}{} and {}{}\nexport MY_API_KEY=hunter2\nPWD=/home/me\n", "ghp", "_abcdefghijklmnopqrstuvwxyz0123456789", "AKIA", "ABCDEFGHIJKLMNOP");
        let k = kinds(&text);
        assert_eq!(k.iter().map(|(a, _)| *a).collect::<Vec<_>>(), ["GitHub token", "AWS access key", "Env credential"]);
        assert_eq!(k[2].1, "export MY_API_KEY=hunter2");
    }

    #[test]
    fn code_values_hashes_dates_and_word_names_are_not_secrets() {
        assert!(kinds("password=None\nTOKEN=''\nsecret=Condition(lambda: 1)\n").is_empty());
        assert!(kinds("commit 4f9c0a1b2c3d4e5f60718293a4b5c6d7e8f90123 20260710_211200_report double-Esc-within-2s").is_empty());
    }

    #[test]
    fn random_tokens_are_caught_and_redacted() {
        let m = scan("key: aB3xY7qLmZ2pQ8rT5vW9nK1 end", &HashSet::new());
        assert_eq!(m.len(), 1);
        assert_eq!(m[0].kind, "High-entropy token");
        assert_eq!(redact("key: aB3xY7qLmZ2pQ8rT5vW9nK1 end", &m), "key: <secret> end");
        assert_eq!(preview(&m, 5), "High-entropy token");
    }

    #[test]
    fn allowlist_hash_drops_a_confirmed_false_positive() {
        let t = "aB3xY7qLmZ2pQ8rT5vW9nK1";
        let allow: HashSet<String> = [hash_value(t)].into();
        assert!(scan(t, &allow).is_empty());
    }

    #[test]
    fn nul_separated_environ_and_private_key_blocks() {
        assert_eq!(kinds("HOME=/root\0SECRET_TOKEN=abc123\0X=1").len(), 1);
        let pk = concat!("-----BEGIN RSA ", "PRIVATE KEY-----\nMIIabc\n-----END RSA ", "PRIVATE KEY-----");
        assert_eq!(kinds(pk)[0].0, "Private key block");
    }

    #[test]
    fn redact_never_leaves_the_tail_of_a_nested_match() {
        let t = "0123456789";
        let ms = [
            Match { kind: "a", start: 0, end: 8, text: t[0..8].into() },
            Match { kind: "b", start: 2, end: 5, text: t[2..5].into() },
        ];
        assert_eq!(redact(t, &ms), "<secret>89");
    }
}
