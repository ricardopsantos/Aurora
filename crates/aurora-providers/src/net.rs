//! Host classification (Python `_is_lan_host`, `_is_bare_ip`).

use std::net::IpAddr;

fn host_of(base_url: &str) -> String {
    url::Url::parse(base_url)
        .ok()
        .and_then(|u| u.host_str().map(|h| h.trim_matches(['[', ']']).trim_end_matches('.').to_lowercase()))
        .unwrap_or_default()
}

fn private_or_loopback(ip: IpAddr) -> bool {
    // R558: `::ffff:192.168.1.5` is the IPv4 host it maps
    let ip = match ip {
        IpAddr::V6(v6) => v6.to_ipv4_mapped().map_or(ip, IpAddr::V4),
        v4 => v4,
    };
    match ip {
        IpAddr::V4(v4) => v4.is_private() || v4.is_loopback() || v4.is_link_local(),
        IpAddr::V6(v6) => v6.is_loopback() || (v6.segments()[0] & 0xfe00) == 0xfc00 || (v6.segments()[0] & 0xffc0) == 0xfe80,
    }
}

/// A server on this machine, the LAN or a VPN: probed before use, and given a
/// short connect budget so a dead one fails over fast.
pub fn is_lan_host(base_url: &str) -> bool {
    let host = host_of(base_url);
    if host.is_empty() || host == "localhost" || host.ends_with(".local") || host.ends_with(".ts.net") {
        return true;
    }
    match host.parse::<IpAddr>() {
        Ok(ip @ IpAddr::V4(v4)) => private_or_loopback(ip) || (v4.octets()[0] == 100 && (v4.octets()[1] & 0xc0) == 64),
        Ok(ip @ IpAddr::V6(v6)) => {
            private_or_loopback(ip) || v6.to_ipv4_mapped().is_some_and(|v4| v4.octets()[0] == 100 && (v4.octets()[1] & 0xc0) == 64)
        }
        Err(_) => false,
    }
}

/// A literal private/loopback IP: a LAN proxy's certificate names its host
/// name, never the bare IP, so TLS verification is skipped for that case only.
pub fn is_bare_private_ip(base_url: &str) -> bool {
    host_of(base_url).parse::<IpAddr>().is_ok_and(private_or_loopback)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_like_python() {
        for lan in ["http://localhost:8080/v1", "http://127.0.0.1:8080", "http://10.0.0.5/v1", "https://box.local/v1",
                    "https://box.example.ts.net/v1", "http://100.70.1.2:8080/v1", "http://[::1]:8080"] {
            assert!(is_lan_host(lan), "{lan}");
        }
        for remote in ["https://openrouter.ai/api/v1", "https://8.8.8.8/v1", "http://100.128.0.1/v1"] {
            assert!(!is_lan_host(remote), "{remote}");
        }
        assert!(is_bare_private_ip("https://192.168.0.10:8443/v1"));
        assert!(!is_bare_private_ip("https://box.example.ts.net/v1"));
    }

    #[test]
    fn mapped_v6_and_trailing_dots() {
        assert!(is_lan_host("http://[::ffff:192.168.1.5]:8080/v1"));
        assert!(is_lan_host("http://[::ffff:100.70.1.2]/v1"));
        assert!(is_bare_private_ip("https://[::ffff:10.0.0.2]/v1"));
        assert!(!is_lan_host("http://[::ffff:8.8.8.8]/v1"));
        assert!(is_lan_host("http://localhost.:8080/v1"));
        assert!(is_lan_host("https://box.local./v1"));
    }
}
