//! Python counts string length in code points; these helpers keep the Rust
//! port's limits and truncation notices byte-for-byte equal to Python's.

pub fn char_len(s: &str) -> usize {
    s.chars().count()
}

/// The first `n` code points of `s` (Python `s[:n]`).
pub fn head(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

/// The last `n` code points of `s` (Python `s[len(s)-n:]`; R561: `n = 0` is "", it used to panic).
pub fn tail(s: &str, n: usize) -> &str {
    if n == 0 {
        return "";
    }
    let len = char_len(s);
    if n >= len {
        return s;
    }
    let (i, _) = s.char_indices().nth(len - n).expect("index inside the string");
    &s[i..]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_code_points_not_bytes() {
        assert_eq!(char_len("ação"), 4);
        assert_eq!(head("ação", 2), "aç");
        assert_eq!(tail("ação", 2), "ão");
        assert_eq!(head("ab", 5), "ab");
        assert_eq!(tail("ab", 5), "ab");
    }

    #[test]
    fn zero_and_empty_never_panic() {
        assert_eq!(tail("ação", 0), "");
        assert_eq!(tail("", 0), "");
        assert_eq!(head("", 3), "");
        assert_eq!(head("ação", 0), "");
        assert_eq!(char_len(""), 0);
    }
}
