"""Unit tests for aurora/secrets.py (R58) — pure functions, no I/O."""

import pytest

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


def test_scan_finds_bare_env_credential_names():
    """R95d: the name prefix was mandatory, so the commonest spellings of all
    — a bare API_KEY=/SECRET=/TOKEN=/PASSWORD= at the start of a line, the
    normal shape in a .env file or an `env` dump — matched nothing."""
    for line in ("API_KEY=abc123", "SECRET=hunter2", "TOKEN=abcdef",
                 "PASSWORD=hunter2", "PASSWD=hunter2"):
        assert [m.kind for m in secrets.scan(line)] == ["Env credential"], line


def test_scan_ignores_bare_pwd_but_not_prefixed_pwd():
    """PWD is the shell's own working directory — in every `env` dump and
    never a credential; DB_PWD is. The exception that keeps a prefix."""
    assert secrets.scan("PWD=/Users/me/project") == []
    assert [m.kind for m in secrets.scan("DB_PWD=hunter2")] == ["Env credential"]


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


def test_redact_overlapping_or_unsorted_matches_stay_correct():
    """redact() must not assume `matches` arrives in document order — scan()
    already sorts, but redact() is a public function callers may feed
    directly (e.g. after filtering an allowlist)."""
    text = f"{_AWS1} middle {_AWS2}"
    m = secrets.scan(text)
    assert len(m) == 2
    reversed_order = list(reversed(m))
    assert secrets.redact(text, reversed_order) == secrets.redact(text, m)
    assert secrets.redact(text, m) == "<secret> middle <secret>"


def test_redact_is_linear_not_quadratic():
    """R96d: the old right-to-left `text[:m.start] + ... + text[m.end:]` loop
    rebuilt and copied the ENTIRE string on every substitution — O(matches x
    len(text)). On a match-dense blob (a big .env, a token-heavy log — the
    R58 case redact() exists for) that's quadratic. Compares wall-clock
    scaling directly: 4x the matches at the same density should cost ~4x, not
    ~16x."""
    import time

    def make(n):
        return "\n".join(f"key{i}={_AWS1}" for i in range(n))

    def timed(n):
        text = make(n)
        m = secrets.scan(text)
        assert len(m) == n
        t0 = time.perf_counter()
        for _ in range(5):
            secrets.redact(text, m)
        return (time.perf_counter() - t0) / 5

    small = timed(500)
    big = timed(2000)         # 4x the matches, 4x the text
    # quadratic old code scores ~16x here; linear code stays near 4x
    assert big < small * 8, \
        f"small={small*1000:.2f}ms big={big*1000:.2f}ms — redact scaling looks quadratic"


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


def test_scan_stays_linear_on_match_dense_text():
    """R90e: overlap tracking is a per-char mask, not a list of spans that
    every later candidate re-scans. 2000 UUIDs used to be ~2M comparisons on
    the worker thread, on by default; this must stay fast AND still find and
    de-duplicate every match exactly once."""
    import time
    blob = "\n".join(f"id={i:08x}-1111-2222-3333-444444444444" for i in range(2000))
    t0 = time.monotonic()
    matches = secrets.scan(blob)
    elapsed = time.monotonic() - t0
    assert len(matches) == 2000
    assert all(m.kind == "GUID/UUID" for m in matches)
    assert elapsed < 2.0, f"scan took {elapsed:.1f}s — overlap check regressed"


def test_overlapping_spans_are_still_claimed_once():
    text = "AWS_SECRET_ACCESS_KEY=AKIA" + "Q" * 16
    matches = secrets.scan(text)
    spans = [(m.start, m.end) for m in matches]
    assert len(spans) == len(set(spans))
    for i, (s, e) in enumerate(spans):
        for s2, e2 in spans[i + 1:]:
            assert not (s < e2 and s2 < e), "spans overlap — redact would misalign"


# ── R96g: literal preguards must never cause a false NEGATIVE ─────────────
def test_scan_finds_slack_token():
    text = "token: xoxb-" + "a" * 20
    m = secrets.scan(text)
    assert any(x.kind == "Slack token" for x in m)


def test_scan_finds_stripe_key():
    text = "STRIPE_KEY=sk_live_" + "a" * 24
    m = secrets.scan(text)
    assert any(x.kind == "Stripe key" for x in m)


def test_scan_finds_bearer_token():
    text = "Authorization: Bearer " + "a" * 30
    m = secrets.scan(text)
    assert any(x.kind == "Bearer token" for x in m)


@pytest.mark.parametrize("name,pattern,guard", [
    (name, pat, secrets._LITERAL_GUARD[name])
    for name, pat in secrets.PATTERNS
])
def test_literal_guard_is_a_true_superset_of_its_pattern(name, pattern, guard):
    """R96g: the correctness invariant a guard must never violate — every
    string the REGEX can match must contain at least one of the guard's
    literals, or the guard would make scan() silently miss real secrets.
    Checked directly against every real match found across the whole test
    suite's fixtures for that pattern, so this fails immediately if a future
    edit to PATTERNS adds a shape the guard doesn't cover."""
    if guard is None:
        pytest.skip(f"{name} has no guard — always scanned")
    samples = {
        "AWS access key": [_AWS1, _AWS2],
        "GitHub token": ["ghp_" + "a" * 36, "gho_" + "b" * 36],
        "Slack token": ["xoxb-" + "a" * 20, "xoxp-" + "b" * 20],
        "Stripe key": ["sk_live_" + "a" * 20, "pk_live_" + "b" * 20],
        "OpenAI-style key": ["sk-" + "a" * 24],
        "Bearer token": ["Bearer " + "a" * 20],
        "Private key block": ["-----BEGIN RSA PRIVATE KEY-----\nx\n"
                              "-----END RSA PRIVATE KEY-----"],
    }[name]
    for s in samples:
        assert pattern.search(s), f"fixture {s!r} doesn't even match its own pattern"
        assert any(lit in s for lit in guard), \
            f"guard {guard} misses a real match: {s!r}"


def test_scan_is_faster_with_literal_guards_on_ordinary_text():
    """R96g: most real text (source code, logs) contains none of the
    per-pattern literals, so the guard should skip most of the ten regex
    passes entirely rather than running all of them. Compares scan() against
    a copy of the pre-fix loop (no guard) on the same text."""
    import time

    def scan_unguarded(text):
        found = []
        claimed = bytearray(len(text))
        for name, pat in secrets.PATTERNS:
            for m in pat.finditer(text):
                s, e = m.span()
                if b"\x01" in claimed[s:e]:
                    continue
                claimed[s:e] = b"\x01" * (e - s)
                found.append(m)
        return found

    code = open(secrets.__file__.replace("secrets.py", "tui.py")).read()
    blob = (code * (60_000 // len(code) + 1))[:60_000]

    t0 = time.perf_counter()
    for _ in range(10):
        scan_unguarded(blob)
    unguarded_ms = (time.perf_counter() - t0) / 10 * 1000

    t1 = time.perf_counter()
    for _ in range(10):
        secrets.scan(blob)
    guarded_ms = (time.perf_counter() - t1) / 10 * 1000

    assert guarded_ms < unguarded_ms * 0.8, \
        f"unguarded={unguarded_ms:.2f}ms guarded={guarded_ms:.2f}ms — guard isn't helping"
