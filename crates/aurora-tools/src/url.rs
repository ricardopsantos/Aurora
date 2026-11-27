//! R283/R300: does a web_fetch URL need approval (Python `tools.url_needs_approval`)?
//! Decided without any DNS query: resolving a name can itself leak data.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// `socket.inet_aton`: a, a.b, a.b.c, a.b.c.d; each part decimal, octal (0…) or hex (0x…).
fn inet_aton(s: &str) -> Option<Ipv4Addr> {
    let parts: Vec<&str> = s.split('.').collect();
    if parts.is_empty() || parts.len() > 4 {
        return None;
    }
    let num = |p: &str| -> Option<u64> {
        if p.is_empty() {
            return None;
        }
        if let Some(h) = p.strip_prefix("0x").or_else(|| p.strip_prefix("0X")) {
            return if h.is_empty() { Some(0) } else { u64::from_str_radix(h, 16).ok() };
        }
        if p.len() > 1 && p.starts_with('0') {
            return u64::from_str_radix(&p[1..], 8).ok();
        }
        p.parse().ok()
    };
    let vals: Vec<u64> = parts.iter().map(|p| num(p)).collect::<Option<_>>()?;
    let (last, front) = vals.split_last()?;
    if front.iter().any(|v| *v > 255) {
        return None;
    }
    let max = 1u64 << (8 * (4 - front.len()));
    if *last >= max {
        return None;
    }
    let mut n: u64 = 0;
    for (i, v) in front.iter().enumerate() {
        n |= v << (8 * (3 - i));
    }
    Some(Ipv4Addr::from((n | last) as u32))
}

fn in4(a: Ipv4Addr, net: [u8; 4], bits: u32) -> bool {
    let mask = if bits == 0 { 0 } else { u32::MAX << (32 - bits) };
    (u32::from(a) & mask) == (u32::from(Ipv4Addr::from(net)) & mask)
}

/// Python `ipaddress` `is_global` (3.11 tables).
fn v4_global(a: Ipv4Addr) -> bool {
    const PRIVATE: [([u8; 4], u32); 14] = [
        ([0, 0, 0, 0], 8), ([10, 0, 0, 0], 8), ([127, 0, 0, 0], 8), ([169, 254, 0, 0], 16), ([172, 16, 0, 0], 12),
        ([192, 0, 0, 0], 29), ([192, 0, 0, 170], 31), ([192, 0, 2, 0], 24), ([192, 168, 0, 0], 16), ([198, 18, 0, 0], 15),
        ([198, 51, 100, 0], 24), ([203, 0, 113, 0], 24), ([240, 0, 0, 0], 4), ([255, 255, 255, 255], 32),
    ];
    !in4(a, [100, 64, 0, 0], 10) && !PRIVATE.iter().any(|(n, b)| in4(a, *n, *b))
}

fn v6_global(a: Ipv6Addr) -> bool {
    if let Some(m) = a.to_ipv4_mapped() {
        return v4_global(m);
    }
    let s = a.segments();
    let n = u128::from(a);
    let inn = |net: u128, bits: u32| (n >> (128 - bits)) == (net >> (128 - bits));
    let private = a.is_loopback()
        || a.is_unspecified()
        || inn(0x0100 << 112, 64)
        || inn(0x2001 << 112, 23)
        || inn((0x2001u128 << 112) | (0x2 << 96), 48)
        || inn((0x2001u128 << 112) | (0xdb8 << 96), 32)
        || inn((0x2001u128 << 112) | (0x10 << 96), 28)
        || (s[0] & 0xfe00) == 0xfc00
        || (s[0] & 0xffc0) == 0xfe80;
    !private
}

/// Python `_private_host(host, resolve=False)`.
pub fn private_host(host: &str) -> bool {
    // R565: a fully qualified `localhost.` or `nas.local.` is the same host; the dot hid it from the name checks
    let host = host.trim_matches(|c| c == '[' || c == ']').trim_end_matches('.').to_lowercase();
    if host.is_empty() {
        return true;
    }
    if host == "localhost" || [".local", ".ts.net", ".internal", ".localhost", ".lan", ".home"].iter().any(|s| host.ends_with(s)) {
        return true;
    }
    let addr: Option<IpAddr> = host.parse::<IpAddr>().ok().or_else(|| inet_aton(&host).map(IpAddr::V4));
    match addr {
        Some(IpAddr::V4(a)) => !v4_global(a),
        Some(IpAddr::V6(a)) => !v6_global(a),
        None => !host.contains('.'),
    }
}

fn unquote(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Some(v) = std::str::from_utf8(&b[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok()) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

const SECRET_PREFIXES: [&str; 10] = ["AKIA", "ASIA", "ghp_", "gho_", "github_pat_", "sk-", "xox", "glpat-", "AIza", "-----BEGIN"];

fn data_seg(seg: &str) -> bool {
    seg.chars().count() >= 40 && seg.chars().all(|c| c.is_ascii_alphanumeric() || "+/=_%.-".contains(c))
}

fn hex_hash(seg: &str) -> bool {
    (seg.len() == 40 || seg.len() == 64) && seg.chars().all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
}

/// R283: a non-http(s) scheme, a query string, a non-public host or a payload-shaped URL asks first.
pub fn url_needs_approval(url: &str) -> bool {
    let u = url.trim();
    let Some((scheme, rest)) = u.split_once(':') else { return true };
    let scheme = scheme.to_lowercase();
    if scheme != "http" && scheme != "https" {
        return true;
    }
    let Some(rest) = rest.strip_prefix("//") else {
        // no netloc: no hostname, which is private
        return true;
    };
    let (before_frag, _frag) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, query) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let (netloc, path) = match before_query.find('/') {
        Some(i) => (&before_query[..i], &before_query[i..]),
        None => (before_query, ""),
    };
    let hostport = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let host = if hostport.starts_with('[') {
        hostport.split_once(']').map_or(hostport, |(h, _)| h).trim_start_matches('[')
    } else {
        hostport.split_once(':').map_or(hostport, |(h, _)| h)
    };
    if !query.is_empty() || private_host(host) {
        return true;
    }
    if u.chars().count() > 200 {
        return true;
    }
    if host.split('.').any(|l| l.chars().count() >= 30) {
        return true;
    }
    for seg in unquote(path).split('/') {
        if data_seg(seg) && !hex_hash(seg) {
            return true;
        }
        if seg.chars().count() >= 12 && SECRET_PREFIXES.iter().any(|p| seg.to_lowercase().contains(&p.to_lowercase())) {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trailing_dot_names_are_still_private() {
        for h in ["localhost.", "nas.local.", "box.example.ts.net.", "127.0.0.1.", "[::1]"] {
            assert!(private_host(h), "{h}");
        }
        assert!(!private_host("example.com."));
        assert!(url_needs_approval("http://localhost./admin"));
    }

    #[test]
    fn public_pages_pass_and_the_rest_ask() {
        assert!(!url_needs_approval("https://example.org/docs/page.html"));
        for u in [
            "https://example.org/?q=1",
            "http://localhost:8080/",
            "http://127.1/",
            "http://0x7f000001/",
            "http://[::1]/",
            "http://10.0.0.5/",
            "http://169.254.169.254/latest",
            "http://intranet/",
            "ftp://example.org/",
            "https://box.example.ts.net/",
        ] {
            assert!(url_needs_approval(u), "{u}");
        }
        let fake_key = format!("https://example.org/{}{}", "AK", "IAABCDEFGHIJKLMNOP");
        assert!(url_needs_approval(&fake_key));
        assert!(!url_needs_approval("https://github.com/a/b/commit/0123456789abcdef0123456789abcdef01234567"));
    }
}
