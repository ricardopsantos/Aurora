"""Unit tests for the inline newline expansion helper in aurora/ui.py."""

from aurora.ui import _expand_newlines, _expand_typed_newline


def test_simple_backslash_n():
    assert _expand_newlines("hello \\n world") == "hello \n world"


def test_simple_backslash_br():
    assert _expand_newlines("a \\br b") == "a \n b"


def test_multiple_tokens():
    assert _expand_newlines("a\\nb\\nc") == "a\nb\nc"


def test_leading_and_trailing_tokens():
    assert _expand_newlines("\\nhello") == "\nhello"
    assert _expand_newlines("hello\\n") == "hello\n"


def test_adjacent_to_punctuation():
    assert _expand_newlines("(\\n)") == "(\n)"
    assert _expand_newlines("hello.\\nworld") == "hello.\nworld"


def test_forward_slash_n_is_not_a_newline():
    # only *backslash* sequences expand; forward-slash /n and /br are literal
    assert _expand_newlines("path/neee") == "path/neee"
    assert _expand_newlines("a /br b") == "a /br b"


def test_doubled_backslash_is_literal():
    # \\\\n and \\\\br keep one literal backslash in front of the token
    assert _expand_newlines("hello \\\\n world") == "hello \\n world"
    assert _expand_newlines("a \\\\br b") == "a \\br b"


def test_no_other_sequences_changed():
    assert _expand_newlines("/n /br \\t \\r") == "/n /br \\t \\r"


def test_empty_and_no_tokens_identity():
    assert _expand_newlines("") == ""
    assert _expand_newlines("plain text") == "plain text"


class _Buf:
    def __init__(self, text="", pos=0):
        self.text = text
        self.cursor_position = pos

    def insert_text(self, s):
        self.text = self.text[:self.cursor_position] + s + self.text[self.cursor_position:]
        self.cursor_position += len(s)

    def delete(self, count):
        self.text = self.text[:self.cursor_position] + self.text[self.cursor_position + count:]


def test_typed_newline_backslash_n_plus_space():
    text = "hello " + chr(92) + "n"   # literal: hello \n
    b = _Buf(text, len(text))
    assert _expand_typed_newline(b) is True
    assert b.text == "hello \n"
    assert b.cursor_position == 7


def test_typed_newline_backslash_br_plus_space():
    text = "a " + chr(92) + "br"       # literal: a \br
    b = _Buf(text, len(text))
    assert _expand_typed_newline(b) is True
    assert b.text == "a \n"
    assert b.cursor_position == 3


def test_typed_newline_double_backslash_left_literal():
    text = "hello " + chr(92) * 2 + "n"  # literal: hello \\n
    b = _Buf(text, len(text))
    assert _expand_typed_newline(b) is False
    assert b.text == text


def test_typed_newline_no_token():
    b = _Buf("hello ", 6)
    assert _expand_typed_newline(b) is False
