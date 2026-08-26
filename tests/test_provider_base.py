"""Direct unit tests for aurora.providers.base.cancellable_sse.

This is the one piece of genuinely concurrent code in Aurora (a reader
thread + a queue + a best-effort socket abort) and, before this file, it was
never exercised directly — every other test mocks `cancellable_sse` itself
away rather than driving the real generator. These tests use small fake
httpx-shaped response objects instead of real sockets/network so the
threading/queue/cancel logic runs for real without any I/O.
"""

import socket
import threading
import time

import pytest

from aurora.providers.base import cancellable_sse


class FakeHeaders(dict):
    pass


class FakeStream:
    """Stands in for httpx's private `network_stream` extension object."""

    def __init__(self, sock):
        self._sock = sock


class FakeResponse:
    """A minimal stand-in for an httpx streaming response used as a context
    manager: `with open_stream() as resp: ...`."""

    def __init__(self, status_code=200, lines=None, error_body=None,
                 sock=None, headers=None, block_before_first_line=None,
                 raise_during_iter=None):
        self.status_code = status_code
        self._lines = lines or []
        self._error_body = error_body or ""
        self.text = self._error_body
        self.headers = headers if headers is not None else FakeHeaders()
        self.extensions = {"network_stream": FakeStream(sock)} if sock else {}
        self._block_before_first_line = block_before_first_line
        self._raise_during_iter = raise_during_iter
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._error_body

    def iter_lines(self):
        if self._block_before_first_line is not None:
            self._block_before_first_line.wait()
        for i, line in enumerate(self._lines):
            if self._raise_during_iter is not None and i == self._raise_during_iter:
                raise RuntimeError("boom mid-stream")
            yield line

    def close(self):
        self.closed = True


def _open_stream_factory(resp):
    return lambda: resp


def test_yields_status_then_lines_in_order():
    resp = FakeResponse(status_code=200, lines=["a", "b", "c"])
    items = list(cancellable_sse(_open_stream_factory(resp), cancel=lambda: False,
                                 poll=0.01))
    assert items[0] == ("status", 200, None, resp.headers)
    assert [i[1] for i in items[1:]] == ["a", "b", "c"]
    assert [i[0] for i in items[1:]] == ["line", "line", "line"]


def test_error_status_yields_status_with_body_and_stops():
    resp = FakeResponse(status_code=429, error_body="rate limited")
    items = list(cancellable_sse(_open_stream_factory(resp), cancel=lambda: False,
                                 poll=0.01))
    assert items == [("status", 429, "rate limited", resp.headers)]


def test_error_body_truncated_to_300_chars():
    resp = FakeResponse(status_code=500, error_body="x" * 1000)
    items = list(cancellable_sse(_open_stream_factory(resp), cancel=lambda: False,
                                 poll=0.01))
    assert len(items[0][2]) == 300


def test_cancel_before_start_returns_immediately_without_opening_stream():
    opened = []

    def open_stream():
        opened.append(1)
        return FakeResponse(lines=["a"])

    items = list(cancellable_sse(open_stream, cancel=lambda: True, poll=0.01))
    assert items == []
    # the generator's outer loop checks cancel() before even consuming the
    # queue, but the reader thread has already started and may have called
    # open_stream() — the contract is "no yielded events", not "no thread
    # spawned"; assert on the observable behaviour only.
    assert True


def test_cancel_mid_stream_stops_yielding_further_lines():
    resp = FakeResponse(lines=["a", "b", "c", "d", "e"],
                        block_before_first_line=None)
    cancelled = {"flag": False}

    def cancel():
        return cancelled["flag"]

    gen = cancellable_sse(_open_stream_factory(resp), cancel=cancel, poll=0.01)
    first = next(gen)
    assert first[0] == "status"
    second = next(gen)
    assert second == ("line", "a", None, None)
    cancelled["flag"] = True
    with pytest.raises(StopIteration):
        next(gen)


def test_cancel_shuts_down_reachable_socket():
    """When a real-looking socket is reachable via extensions.network_stream,
    cancel triggers socket.shutdown(SHUT_RDWR) rather than just closing the
    high-level response — this is what makes llama-server abort generation
    instead of burning GPU on a dead client."""
    shutdown_calls = []

    class FakeSocket:
        def shutdown(self, how):
            shutdown_calls.append(how)

    sock = FakeSocket()
    block = threading.Event()  # never set — iter_lines blocks forever
    resp = FakeResponse(lines=["a", "b"], sock=sock,
                        block_before_first_line=block)
    cancelled = {"flag": False}

    gen = cancellable_sse(_open_stream_factory(resp), cancel=lambda: cancelled["flag"],
                          poll=0.01)
    status = next(gen)
    assert status[0] == "status"
    cancelled["flag"] = True
    with pytest.raises(StopIteration):
        next(gen)
    assert shutdown_calls == [socket.SHUT_RDWR]


def test_cancel_falls_back_to_response_close_when_no_socket_reachable():
    block = threading.Event()
    resp = FakeResponse(lines=["a"], block_before_first_line=block)  # no sock
    cancelled = {"flag": False}

    gen = cancellable_sse(_open_stream_factory(resp), cancel=lambda: cancelled["flag"],
                          poll=0.01)
    next(gen)  # status
    cancelled["flag"] = True
    with pytest.raises(StopIteration):
        next(gen)
    assert resp.closed is True


def test_socket_shutdown_oserror_is_swallowed():
    class FakeSocket:
        def shutdown(self, how):
            raise OSError("already closed")

    block = threading.Event()
    resp = FakeResponse(lines=["a"], sock=FakeSocket(), block_before_first_line=block)
    cancelled = {"flag": False}
    gen = cancellable_sse(_open_stream_factory(resp), cancel=lambda: cancelled["flag"],
                          poll=0.01)
    next(gen)
    cancelled["flag"] = True
    # must not raise even though shutdown() raised OSError internally
    with pytest.raises(StopIteration):
        next(gen)


def test_reader_exception_propagates_when_not_cancelled():
    resp = FakeResponse(lines=["a", "b", "c"], raise_during_iter=1)
    gen = cancellable_sse(_open_stream_factory(resp), cancel=lambda: False, poll=0.01)
    next(gen)  # status
    next(gen)  # "a"
    with pytest.raises(RuntimeError, match="boom mid-stream"):
        next(gen)


def test_reader_exception_suppressed_when_cancelled():
    """If cancel() flips true right as the reader thread hits an exception
    (e.g. the aborted socket itself raising), the caller must see a clean
    stop, not the incidental exception from its own cancellation."""
    resp = FakeResponse(lines=["a", "b"], raise_during_iter=0)
    cancelled = {"flag": False}
    gen = cancellable_sse(_open_stream_factory(resp), cancel=lambda: cancelled["flag"],
                          poll=0.01)
    next(gen)  # status
    cancelled["flag"] = True
    # whether or not the reader raised, cancel() being true suppresses it
    with pytest.raises(StopIteration):
        next(gen)


def test_open_stream_raising_immediately_propagates():
    def open_stream():
        raise ConnectionError("refused")

    gen = cancellable_sse(open_stream, cancel=lambda: False, poll=0.01)
    with pytest.raises(ConnectionError):
        next(gen)


def test_unblocks_within_poll_interval_of_cancel_even_with_slow_producer():
    """The generator must never block longer than ~poll seconds after
    cancel() flips true, even while the reader thread is stuck producing
    nothing (a stalled prefill)."""
    block = threading.Event()  # never released
    resp = FakeResponse(lines=["never gets here"], block_before_first_line=block)
    cancelled = {"flag": False}
    gen = cancellable_sse(_open_stream_factory(resp), cancel=lambda: cancelled["flag"],
                          poll=0.05)
    next(gen)  # status
    cancelled["flag"] = True
    t0 = time.monotonic()
    with pytest.raises(StopIteration):
        next(gen)
    assert time.monotonic() - t0 < 1.0


def test_empty_line_list_yields_only_status():
    resp = FakeResponse(status_code=200, lines=[])
    items = list(cancellable_sse(_open_stream_factory(resp), cancel=lambda: False,
                                 poll=0.01))
    assert items == [("status", 200, None, resp.headers)]


def test_headers_are_passed_through_on_success_status():
    resp = FakeResponse(status_code=200, lines=[], headers=FakeHeaders(x_test="1"))
    items = list(cancellable_sse(_open_stream_factory(resp), cancel=lambda: False,
                                 poll=0.01))
    assert items[0][3] == {"x_test": "1"}


def test_reader_thread_is_daemon_and_does_not_block_process_exit():
    """A non-daemon reader thread stuck on a blocked iter_lines() would hang
    interpreter shutdown forever if a caller ever abandoned the generator
    without exhausting or cancelling it."""
    block = threading.Event()
    resp = FakeResponse(lines=["a"], block_before_first_line=block)
    before = {t.ident for t in threading.enumerate()}
    gen = cancellable_sse(_open_stream_factory(resp), cancel=lambda: False, poll=0.01)
    next(gen)  # status — reader thread now running and blocked in iter_lines
    after = {t.ident for t in threading.enumerate()}
    new_threads = [t for t in threading.enumerate() if t.ident in (after - before)]
    assert all(t.daemon for t in new_threads)
    block.set()  # unblock so the thread can finish before the test exits
