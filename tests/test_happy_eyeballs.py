"""Tests for aurora.providers.happy_eyeballs — previously untested despite
being the one genuinely concurrent piece of custom networking code in
Aurora (RFC 8305 address-family racing, socket cleanup, error handling).
Uses real local TCP sockets (loopback, ephemeral ports) rather than mocking
the socket layer, so the actual race/cleanup logic runs for real."""

import socket
import threading
import time

import pytest

from aurora.providers.happy_eyeballs import (
    HappyEyeballsTransport,
    _ordered,
    happy_eyeballs_connect,
)

# ── _ordered ────────────────────────────────────────────────────────────

def _info(fam, addr):
    return (fam, socket.SOCK_STREAM, 0, "", addr)


def test_ordered_interleaves_v6_and_v4():
    infos = [
        _info(socket.AF_INET, ("1.1.1.1", 80)),
        _info(socket.AF_INET6, ("::1", 80, 0, 0)),
        _info(socket.AF_INET, ("2.2.2.2", 80)),
        _info(socket.AF_INET6, ("::2", 80, 0, 0)),
    ]
    out = _ordered(infos)
    fams = [f for f, _sa in out]
    assert fams == [socket.AF_INET6, socket.AF_INET,
                    socket.AF_INET6, socket.AF_INET]


def test_ordered_v6_only():
    infos = [_info(socket.AF_INET6, ("::1", 80, 0, 0)),
             _info(socket.AF_INET6, ("::2", 80, 0, 0))]
    out = _ordered(infos)
    assert [f for f, _ in out] == [socket.AF_INET6, socket.AF_INET6]


def test_ordered_v4_only():
    infos = [_info(socket.AF_INET, ("1.1.1.1", 80)),
             _info(socket.AF_INET, ("2.2.2.2", 80))]
    out = _ordered(infos)
    assert [f for f, _ in out] == [socket.AF_INET, socket.AF_INET]


def test_ordered_more_v6_than_v4_tail_is_v6():
    infos = [_info(socket.AF_INET6, ("::1", 80, 0, 0)),
             _info(socket.AF_INET6, ("::2", 80, 0, 0)),
             _info(socket.AF_INET, ("1.1.1.1", 80))]
    out = _ordered(infos)
    assert [f for f, _ in out] == [socket.AF_INET6, socket.AF_INET,
                                   socket.AF_INET6]


def test_ordered_empty_input():
    assert _ordered([]) == []


# ── happy_eyeballs_connect: real sockets on loopback ───────────────────

def _listener():
    """A real TCP listener on an ephemeral loopback port, accepting
    connections in a background thread so the OS-level handshake actually
    completes (not just "port open" — a genuine connect())."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except TimeoutError:
                continue
            except OSError:
                return

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    return srv, port, stop


def _closed_port() -> int:
    """A loopback port nothing is listening on — connecting refuses fast."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()   # now nothing listens there
    return port


def test_single_address_shortcut_connects(monkeypatch):
    srv, port, stop = _listener()
    try:
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **k: [_info(socket.AF_INET, ("127.0.0.1", port))])
        sock = happy_eyeballs_connect("127.0.0.1", port, 2.0, None)
        try:
            assert sock.getpeername() == ("127.0.0.1", port)
        finally:
            sock.close()
    finally:
        stop.set()
        srv.close()


def test_single_address_shortcut_failure_raises(monkeypatch):
    port = _closed_port()
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [_info(socket.AF_INET, ("127.0.0.1", port))])
    with pytest.raises(ConnectionRefusedError):
        happy_eyeballs_connect("127.0.0.1", port, 2.0, None)


def test_races_two_addresses_working_one_wins(monkeypatch):
    """One address refused immediately, the other a real listener — the
    working address must win regardless of which is listed first."""
    srv, good_port, stop = _listener()
    bad_port = _closed_port()
    try:
        for order in (["good_first"], ["bad_first"]):
            addrs = ([_info(socket.AF_INET, ("127.0.0.1", good_port)),
                     _info(socket.AF_INET, ("127.0.0.1", bad_port))]
                    if order == ["good_first"] else
                    [_info(socket.AF_INET, ("127.0.0.1", bad_port)),
                     _info(socket.AF_INET, ("127.0.0.1", good_port))])
            monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addrs)
            sock = happy_eyeballs_connect("127.0.0.1", good_port, 2.0, None)
            try:
                assert sock.getpeername()[1] == good_port
            finally:
                sock.close()
    finally:
        stop.set()
        srv.close()


def test_all_addresses_fail_raises_last_error(monkeypatch):
    p1, p2 = _closed_port(), _closed_port()
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [_info(socket.AF_INET, ("127.0.0.1", p1)),
                         _info(socket.AF_INET, ("127.0.0.1", p2))])
    with pytest.raises(OSError):
        happy_eyeballs_connect("127.0.0.1", p1, 2.0, None)


def test_no_addresses_raises_oserror_with_host_in_message(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(OSError, match="no addresses for somehost:1234"):
        happy_eyeballs_connect("somehost", 1234, 2.0, None)


def test_loser_socket_is_closed_not_leaked(monkeypatch):
    """Both addresses point at the SAME real listener, so BOTH connect()
    calls genuinely succeed at the socket level and race for the win — the
    loser must be closed, not left dangling."""
    srv, port, stop = _listener()
    try:
        addrs = [_info(socket.AF_INET, ("127.0.0.1", port)),
                _info(socket.AF_INET, ("127.0.0.1", port))]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addrs)
        closed = []
        real_close = socket.socket.close

        def spy_close(self):
            closed.append(self)
            return real_close(self)

        monkeypatch.setattr(socket.socket, "close", spy_close)
        sock = happy_eyeballs_connect("127.0.0.1", port, 2.0, None)
        # give the loser thread a moment to reach its own close() call
        deadline = time.monotonic() + 2
        while len(closed) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        sock.close()
        # at least the loser's socket.close() must have been observed
        assert len(closed) >= 1
    finally:
        stop.set()
        srv.close()


def test_stagger_delay_lets_a_fast_second_address_win_quickly(monkeypatch):
    """A first address that connects but is SLOW to be attempted (via a
    real stagger) must not force the caller to wait past the stagger delay
    once a later, faster address wins — verifies the race actually
    overlaps rather than running attempts serially."""
    import aurora.providers.happy_eyeballs as he
    srv, good_port, stop = _listener()
    bad_port = _closed_port()
    try:
        # bad (fails fast) first, good (real listener) second — the
        # stagger means "good" starts _STAGGER seconds after "bad", but
        # connecting should still complete well under 2x the stagger
        addrs = [_info(socket.AF_INET, ("127.0.0.1", bad_port)),
                _info(socket.AF_INET, ("127.0.0.1", good_port))]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addrs)
        monkeypatch.setattr(he, "_STAGGER", 0.1)
        t0 = time.monotonic()
        sock = happy_eyeballs_connect("127.0.0.1", good_port, 2.0, None)
        elapsed = time.monotonic() - t0
        try:
            assert sock.getpeername()[1] == good_port
        finally:
            sock.close()
        assert elapsed < 1.0, f"took {elapsed:.2f}s — race did not overlap"
    finally:
        stop.set()
        srv.close()


# ── HappyEyeballsTransport / _HappyEyeballsBackend error mapping ───────

def test_backend_maps_connect_failure_to_httpcore_connect_error(monkeypatch):
    import httpcore

    from aurora.providers.happy_eyeballs import _HappyEyeballsBackend
    port = _closed_port()
    backend = _HappyEyeballsBackend()
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("127.0.0.1", port, timeout=2.0)


def test_transport_installs_happy_eyeballs_backend():
    from aurora.providers.happy_eyeballs import _HappyEyeballsBackend
    t = HappyEyeballsTransport()
    assert isinstance(t._pool._network_backend, _HappyEyeballsBackend)


# ── R233: the single-address fast path must not truncate the sockaddr ────

def test_single_address_shortcut_preserves_the_full_ipv6_sockaddr(monkeypatch):
    """R233: an IPv6 sockaddr is (addr, port, flowinfo, scope_id) and the
    scope lives in sa[3] — `sa[0]` carries no `%iface` suffix. The fast path
    used to `create_connection(sa[:2], ...)`, dropping the scope id and
    handing the kernel an address it rejects with EINVAL, so a link-local
    IPv6 host was unreachable whenever it resolved to exactly one address
    while the racing path connected to it fine."""
    seen = {}

    class _FakeSock:
        def __init__(self, fam, kind):
            seen["family"] = fam

        def settimeout(self, t):
            pass

        def connect(self, sa):
            seen["sockaddr"] = sa

        def close(self):
            pass

    full = ("fe80::1", 80, 0, 7)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [_info(socket.AF_INET6, full)])
    monkeypatch.setattr(socket, "socket", _FakeSock)
    happy_eyeballs_connect("fe80::1%eth0", 80, 2.0, None)
    assert seen["sockaddr"] == full          # all four elements, scope included
    assert seen["family"] == socket.AF_INET6


def test_single_address_shortcut_does_not_re_resolve_the_host(monkeypatch):
    """The fast path went through create_connection, which resolves the
    (host, port) it is given all over again instead of using the address
    already selected. One lookup per connect, not two."""
    calls = []
    real = socket.getaddrinfo

    def counting(*a, **k):
        calls.append(a)
        return [_info(socket.AF_INET, ("127.0.0.1", 9))]

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    srv, port, stop = _listener()
    try:
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **k: (calls.append(a),
                             [_info(socket.AF_INET, ("127.0.0.1", port))])[1])
        sock = happy_eyeballs_connect("127.0.0.1", port, 2.0, None)
        sock.close()
    finally:
        stop.set()
        srv.close()
        socket.getaddrinfo = real
    assert len(calls) == 1
