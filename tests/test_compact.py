"""Direct unit tests for aurora.compact — history flattening, cut-point
selection, and transcript clipping. Pure functions, no I/O."""

import pytest

from aurora import compact, tokens

# ── _stringify / R222 (content=None) ───────────────────────────────────────

def test_flatten_history_tool_call_only_message_has_no_literal_none():
    """R222: content=None (a tool-calls-only assistant message, the ordinary
    OpenAI-format shape) must never render as the literal text 'None'."""
    msgs = [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "read_file",
                                      "arguments": '{"path": "x.py"}'}}]},
    ]
    out = compact.flatten_history(msgs)
    assert "None" not in out
    assert "[called read_file(" in out


def test_flatten_history_normal_text_message_unaffected():
    msgs = [{"role": "assistant", "content": "hello there"}]
    out = compact.flatten_history(msgs)
    assert "Assistant: hello there" in out


def test_flatten_history_empty_string_content_omits_line_when_no_tool_calls():
    msgs = [{"role": "assistant", "content": ""}]
    out = compact.flatten_history(msgs)
    assert out == ""


def test_flatten_history_none_content_with_no_tool_calls_omits_line():
    """No text and no tool call at all — nothing to show, not 'None'."""
    msgs = [{"role": "assistant", "content": None}]
    out = compact.flatten_history(msgs)
    assert out == ""


def test_msg_tokens_none_content_does_not_overcount():
    """R222 also affects the token estimator — 'None' was 4 bogus chars on
    every tool-calls-only message."""
    with_none = {"role": "assistant", "content": None}
    with_empty = {"role": "assistant", "content": ""}
    assert compact._msg_tokens(with_none) == compact._msg_tokens(with_empty)


def test_flatten_history_dict_content_stringified():
    """A non-str, non-None content (some providers' shape) still stringifies
    via str(), just not the None special case."""
    msgs = [{"role": "assistant", "content": {"weird": "shape"}}]
    out = compact.flatten_history(msgs)
    assert "weird" in out


def test_flatten_history_tool_result_message_formatted_distinctly():
    msgs = [{"role": "tool", "content": "file contents here"}]
    out = compact.flatten_history(msgs)
    assert out == "[tool result: file contents here]"


def test_flatten_history_multiple_tool_calls_all_listed():
    msgs = [{"role": "assistant", "content": None,
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": "{}"}},
                {"function": {"name": "grep", "arguments": '{"q": "x"}'}},
            ]}]
    out = compact.flatten_history(msgs)
    assert "[called read_file(" in out
    assert "[called grep(" in out


# ── is_cut_boundary ─────────────────────────────────────────────────────────

def test_is_cut_boundary_user_message_true():
    assert compact.is_cut_boundary({"role": "user", "content": "hi"})


def test_is_cut_boundary_assistant_with_tool_calls_true():
    assert compact.is_cut_boundary(
        {"role": "assistant", "content": None, "tool_calls": [{}]})


def test_is_cut_boundary_bare_assistant_false():
    assert not compact.is_cut_boundary({"role": "assistant", "content": "hi"})


def test_is_cut_boundary_tool_message_false():
    assert not compact.is_cut_boundary({"role": "tool", "content": "out"})


def test_is_cut_boundary_assistant_with_empty_tool_calls_list_false():
    """An empty list is falsy — bool([]) is False — so this must NOT count
    as carrying tool_calls."""
    assert not compact.is_cut_boundary(
        {"role": "assistant", "content": "hi", "tool_calls": []})


# ── cut_index ────────────────────────────────────────────────────────────

def _user(text):
    return {"role": "user", "content": text}


def _assistant(text):
    return {"role": "assistant", "content": text}


def test_cut_index_empty_history_returns_zero():
    assert compact.cut_index([], keep_recent_tokens=1000) == 0


def test_cut_index_single_message_fits_returns_zero():
    assert compact.cut_index([_user("hi")], keep_recent_tokens=1000) == 0


def test_cut_index_returns_a_valid_boundary_index():
    messages = []
    for i in range(20):
        messages.append(_user(f"question {i} " + "x" * 200))
        messages.append(_assistant(f"answer {i} " + "y" * 200))
    idx = compact.cut_index(messages, keep_recent_tokens=100)
    assert compact.is_cut_boundary(messages[idx])


def test_cut_index_never_separates_tool_call_from_its_result():
    """The chosen index must never point INTO the middle of a round — a
    'tool' message must never be the first kept message."""
    messages = [_user("go")]
    for i in range(15):
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": [{"function": {"name": "read_file",
                                                       "arguments": "x" * 300}}]})
        messages.append({"role": "tool", "content": "y" * 300})
    idx = compact.cut_index(messages, keep_recent_tokens=50)
    assert messages[idx]["role"] != "tool"


def test_cut_index_falls_back_to_earlier_boundary_when_none_found_forward():
    """R158: a single oversized tool result at the very end with no boundary
    after it must still fold everything before the last real boundary,
    rather than giving up and returning 0."""
    messages = [_user("start")]
    messages.append({"role": "assistant", "content": None,
                     "tool_calls": [{"function": {"name": "read_file",
                                                   "arguments": "{}"}}]})
    messages.append({"role": "tool", "content": "z" * 5000})   # huge, no boundary after
    idx = compact.cut_index(messages, keep_recent_tokens=10)
    assert idx != 0 or compact.is_cut_boundary(messages[0])
    assert compact.is_cut_boundary(messages[idx])


def test_cut_index_zero_budget_still_returns_a_boundary_or_zero():
    messages = [_user("a"), _assistant("b")]
    idx = compact.cut_index(messages, keep_recent_tokens=0)
    assert idx == 0 or compact.is_cut_boundary(messages[idx])


# ── clip_transcript ──────────────────────────────────────────────────────

def test_clip_transcript_under_limit_is_unchanged():
    text = "short text"
    assert compact.clip_transcript(text, max_tokens=1000) == text


def test_clip_transcript_over_limit_keeps_head_and_tail():
    text = "HEAD" * 500 + "MIDDLE" * 500 + "TAIL" * 500
    out = compact.clip_transcript(text, max_tokens=50)
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "MIDDLE" not in out
    assert "dropped" in out
    assert len(out) < len(text)


def test_clip_transcript_zero_or_negative_max_tokens_returns_original():
    text = "x" * 1000
    assert compact.clip_transcript(text, max_tokens=0) == text
    assert compact.clip_transcript(text, max_tokens=-5) == text


def test_clip_transcript_dropped_count_is_accurate():
    text = "A" * 1000
    # R229: max_tokens=10 (a 40-char limit) no longer fits head+tail+marker
    # at all, so it now hard-truncates instead. Use a budget that does.
    out = compact.clip_transcript(text, max_tokens=60)
    import re
    m = re.search(r"\[\.\.\. (\d+) characters dropped", out)
    assert m is not None
    dropped = int(m.group(1))
    assert 0 < dropped < len(text)


# ── R229: the clip result must stay inside its own budget ────────────────

@pytest.mark.parametrize("max_tokens", [10, 25, 50, 200, 1000, 5000])
def test_clip_transcript_never_exceeds_the_requested_budget(max_tokens):
    """R229: the elision marker used to be added ON TOP of the limit, so a
    function whose whole job is making a request fit returned more than it
    was asked for — ~90 characters always, which at a small remaining
    budget is most of it."""
    out = compact.clip_transcript("A" * 100_000, max_tokens=max_tokens)
    assert len(out) <= max_tokens * tokens.CHARS_PER_TOKEN


def test_clip_transcript_hard_truncates_when_the_marker_cannot_fit():
    """R229: honour the bound over the shape — a caller this starved needs
    the request to fit more than it needs the explanation."""
    out = compact.clip_transcript("A" * 1000, max_tokens=5)
    assert out == "A" * 20
    assert "dropped" not in out


def test_flatten_history_renders_a_null_tool_result_as_empty():
    """R222's rule applied to the `tool` branch, which kept the raw `.get`
    and rendered the literal string 'None'."""
    out = compact.flatten_history([{"role": "tool", "content": None}])
    assert out == "[tool result: ]"
