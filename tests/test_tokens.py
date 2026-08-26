"""Direct unit tests for aurora.tokens — estimate_tokens/
estimate_tokens_from_chars/fmt_token_count previously had no dedicated
tests despite estimate_tokens being used throughout engine.py's context
accounting."""

from aurora import tokens


def test_estimate_tokens_empty_string():
    assert tokens.estimate_tokens("") == 0


def test_estimate_tokens_uses_four_chars_per_token():
    assert tokens.estimate_tokens("x" * 4) == 1
    assert tokens.estimate_tokens("x" * 8) == 2
    assert tokens.estimate_tokens("x" * 40) == 10


def test_estimate_tokens_floor_divides_a_partial_group():
    assert tokens.estimate_tokens("x" * 5) == 1     # 5 // 4
    assert tokens.estimate_tokens("x" * 7) == 1
    assert tokens.estimate_tokens("x" * 3) == 0


def test_estimate_tokens_from_chars_matches_estimate_tokens():
    for n in (0, 1, 3, 4, 5, 100, 4001):
        assert tokens.estimate_tokens_from_chars(n) == \
            tokens.estimate_tokens("x" * n)


def test_estimate_tokens_from_chars_rejects_no_special_casing():
    """A char COUNT, not a string — must behave identically to the direct
    division, no len() call on the argument (that's the whole reason this
    function exists separately from estimate_tokens, see R74e)."""
    assert tokens.estimate_tokens_from_chars(4000) == 1000
    assert tokens.estimate_tokens_from_chars(0) == 0


def test_chars_per_token_constant_matches_the_estimator():
    assert tokens.estimate_tokens("y" * tokens.CHARS_PER_TOKEN) == 1


def test_fmt_token_count_under_1000_is_plain_integer_string():
    assert tokens.fmt_token_count(0) == "0"
    assert tokens.fmt_token_count(1) == "1"
    assert tokens.fmt_token_count(999) == "999"


def test_fmt_token_count_exact_thousand_has_no_decimal():
    assert tokens.fmt_token_count(1000) == "1k"
    assert tokens.fmt_token_count(2000) == "2k"


def test_fmt_token_count_shows_one_decimal_when_not_exact():
    assert tokens.fmt_token_count(1500) == "1.5k"
    assert tokens.fmt_token_count(2300) == "2.3k"


def test_fmt_token_count_large_values():
    assert tokens.fmt_token_count(65000) == "65k"
    assert tokens.fmt_token_count(128000) == "128k"


def test_fmt_token_count_never_shows_a_trailing_dot_zero():
    for n in (1000, 2000, 10000, 100000):
        assert not tokens.fmt_token_count(n).endswith(".0k")
