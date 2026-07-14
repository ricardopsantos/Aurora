"""Unit tests for aurora/secrets.py (R58) — pure functions, no I/O."""

from aurora import secrets


_AWS1 = "AKIA" + "A1B2C3D4E5F6G7H8"     # 16 chars after the AKIA/ASIA prefix
_AWS2 = "AKIA" + "Z9Y8X7W6V5U4T3S2"


def test_scan_finds_aws_key():
    text = f"export AWS_ACCESS_KEY_ID={_AWS1}"
    m = secrets.scan(text)
    assert any(x.kind == "AWS access key" for x in m)


def test_scan_finds_github_token():
    text = "token: ghp_" + "a" * 36
    m = secrets.scan(text)
    assert m and m[0].kind == "GitHub token"


def test_scan_finds_openai_style_key():
    text = "OPENAI_API_KEY=sk-" + "x" * 24
    m = secrets.scan(text)
    assert any(x.kind == "OpenAI-style key" for x in m)


def test_scan_finds_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----"
    m = secrets.scan(pem)
    assert len(m) == 1 and m[0].kind == "Private key block"


def test_scan_finds_env_style_credential():
    text = "STRIPE_SECRET_KEY=abc123\ncolor=blue"
    m = secrets.scan(text)
    kinds = [x.kind for x in m]
    assert "Env credential" in kinds
    assert sum(1 for x in m if x.kind == "Env credential") == 1  # not the color line


def test_scan_ignores_plain_text():
    assert secrets.scan("just a normal sentence about tokens and keys") == []
    assert secrets.scan("") == []


def test_scan_does_not_double_count_overlap():
    # a Bearer token whose value ALSO looks like an OpenAI-style key must
    # be reported once, not twice (would misalign redact() indices)
    text = "Authorization: Bearer sk-" + "y" * 24
    m = secrets.scan(text)
    spans = [(x.start, x.end) for x in m]
    assert len(spans) == len(set(spans))
    for (s1, e1) in spans:
        for (s2, e2) in spans:
            if (s1, e1) != (s2, e2):
                assert not (s1 < e2 and s2 < e1), "matches overlap"


def test_redact_replaces_every_match():
    text = f"key1={_AWS1} and key2={_AWS2}"
    m = secrets.scan(text)
    assert len(m) == 2
    out = secrets.redact(text, m)
    assert "AKIA" not in out
    assert out.count("<secret>") == 2


def test_redact_preserves_surrounding_text():
    text = "before ghp_" + "a" * 36 + " after"
    m = secrets.scan(text)
    out = secrets.redact(text, m)
    assert out == "before <secret> after"


def test_redact_no_matches_is_identity():
    text = "nothing secret here"
    assert secrets.redact(text, secrets.scan(text)) == text


def test_preview_summarizes_kinds_not_raw_text():
    text = f"{_AWS1} and ghp_" + "b" * 36
    m = secrets.scan(text)
    p = secrets.preview(m)
    assert "AKIA" not in p and "ghp_" not in p        # never echoes the secret
    assert "AWS access key" in p and "GitHub token" in p


# ── entropy fallback (ad-hoc tokens with no known vendor prefix) ──────────
def test_scan_catches_high_entropy_token_with_no_known_prefix():
    # a real-world miss: a random internal-tool token has no vendor shape at
    # all, so only entropy scoring catches it
    text = "Some key for web vpXJ0aK2Jz2T0htqDK7SRjvQ7BghZd1g\n\nHello"
    m = secrets.scan(text)
    assert any(x.kind == "High-entropy token" and x.text == "vpXJ0aK2Jz2T0htqDK7SRjvQ7BghZd1g"
              for x in m)


def test_scan_flags_a_guid_as_a_possible_secret():
    # GUIDs/UUIDs are sometimes used as API keys/session tokens, not just
    # harmless correlation IDs — deliberately flagged, not entropy-excluded
    text = "request id: 3bd5c7b0-b97d-4571-b888-2c5c408560d4"
    m = secrets.scan(text)
    assert len(m) == 1 and m[0].kind == "GUID/UUID"
    assert secrets.redact(text, m) == "request id: <secret>"


def test_scan_entropy_ignores_git_sha_and_hex_hash():
    assert secrets.scan("commit abcdef0123456789abcdef0123456789abcdef01") == []
    assert secrets.scan("checksum: 5d41402abc4b2a76b9719d911017c592") == []


def test_scan_entropy_ignores_prose_and_paths():
    assert secrets.scan("thisisjustareallylongsentencewithnospacesinitatall") == []
    assert secrets.scan("/Users/me/Desktop/some/very/long/directory/name/here") == []


def test_scan_entropy_does_not_double_count_a_vendor_match():
    # a GitHub token is both regex-matched AND long/high-entropy; must appear
    # exactly once, as the specific vendor kind, not also as a generic one
    text = "ghp_" + "aB3" * 13
    m = secrets.scan(text)
    assert len(m) == 1 and m[0].kind == "GitHub token"


def test_preview_counts_repeats():
    aws3 = "AKIA" + "M1M1M1M1M1M1M1M1"
    text = f"{_AWS1} {_AWS2} {aws3}"
    m = secrets.scan(text)
    assert "3× AWS access key" in secrets.preview(m)


def test_format_matches_shows_context_with_bold_token():
    text = f"export AWS_ACCESS_KEY_ID={_AWS1} # staging"
    m = secrets.scan(text)
    lines = secrets.format_matches(text, m)
    assert len(lines) == 1
    assert _AWS1 not in lines[0] or "\x1b[1m" in lines[0]   # raw secret only inside bold
    assert "AWS access key" in lines[0]
    assert "export AWS_ACCESS_KEY_ID=" in lines[0]


def test_scan_ignores_dated_file_paths():
    # Paths with ISO-style timestamps / dated filenames were false positives:
    # '/' made the entropy fallback see the whole path as one mixed token and
    # the digits pushed it above the threshold.
    paths = [
        "user/Desktop/project/.agentic_context/MEMORY/project-context/20260710_211200_token-study-deployment-context.md",
        "user/Desktop/project/.agentic_context/MEMORY/work-todo/20260710_211300_tomorrow-checklist.m",
        "/Users/someone/Desktop/project/.agentic_context/MEMORY/project-paths/20260617_103502_all-local-git-repos.md",
    ]
    for p in paths:
        assert secrets.scan(p) == [], p


def test_scan_drops_allowlisted_match():
    text = f"export AWS_ACCESS_KEY_ID={_AWS1}"
    allowlist = {secrets.hash_value(_AWS1)}
    assert secrets.scan(text, allowlist) == []


def test_scan_allowlist_only_drops_the_matching_value():
    text = f"a={_AWS1} b={_AWS2}"
    allowlist = {secrets.hash_value(_AWS1)}
    m = secrets.scan(text, allowlist)
    assert len(m) == 1 and m[0].text == _AWS2


def test_hash_value_is_stable_and_distinguishes_inputs():
    assert secrets.hash_value(_AWS1) == secrets.hash_value(_AWS1)
    assert secrets.hash_value(_AWS1) != secrets.hash_value(_AWS2)
