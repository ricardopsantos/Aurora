"""Happy Eyeballs (RFC 8305) for httpx sync clients.

Why: a host with both A and AAAA records on a machine whose public IPv6 route
is dead (common when Tailscale is up — public IPv6 blackholes) makes every
connection burn the whole connect timeout stalling on IPv6 before falling back
to IPv4 (measured 17s vs 0.15s against OpenRouter). The correct fix is not to
disable IPv6 (that breaks IPv6-only networks) but to race the address families
and use whichever connects first.

This plugs a custom network backend into httpcore's connection pool, replacing
its single `socket.create_connection` with a staggered, first-wins connect. It
composes with httpcore's own connect-retry (`retries=`), which still handles a
transient failure on the winning family.
"""

import socket
import threading
from itertools import zip_longest
from queue import Queue

import httpx
from httpcore import ConnectError, ConnectTimeout
from httpcore._backends.sync import SyncBackend, SyncStream

# RFC 8305 §5: a small delay before starting the next family's attempt, so a
# reachable family wins quickly without waiting on a stalled one's full timeout.
_STAGGER = 0.25


def _ordered(infos: list) -> list[tuple[int, tuple]]:
    """Addresses interleaved by family (v6, v4, v6, v4, …) per RFC 8305 §4, so
    attempts alternate rather than exhausting one family first."""
    v6 = [(f, sa) for f, _t, _p, _c, sa in infos if f == socket.AF_INET6]
    v4 = [(f, sa) for f, _t, _p, _c, sa in infos if f == socket.AF_INET]
    out: list[tuple[int, tuple]] = []
    for a, b in zip_longest(v6, v4):
        if a:
            out.append(a)
        if b:
            out.append(b)
    return out


def happy_eyeballs_connect(host: str, port: int, timeout: float | None,
                           source_address: tuple | None) -> socket.socket:
    """Return the first socket to connect, racing address families with a
    small stagger. Raises the last error if every attempt fails."""
    addrs = _ordered(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    if not addrs:
        raise OSError(f"no addresses for {host}:{port}")
    if len(addrs) == 1:                       # nothing to race
        fam, sa = addrs[0]
        return socket.create_connection(
            sa[:2], timeout, source_address=source_address)

    # ONE queue for every outcome, so the first ("ok", sock) is picked up the
    # instant it lands — we never block waiting on a slower/stalled family.
    results: Queue = Queue()
    stop = threading.Event()
    win_lock = threading.Lock()   # winner selection must be atomic — a bare
    # is_set()/set() pair lets two connects both "win" and leaks the loser's
    # socket (nobody is left to close it once the caller has returned)

    def attempt(fam: int, sa: tuple, delay: float) -> None:
        if stop.wait(delay):                  # someone already won during stagger
            results.put(("skip", None))
            return
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
            if source_address is not None:
                s.bind(source_address)
            s.connect(sa)
        except Exception as e:                # noqa: BLE001 — report, keep racing
            s.close()
            results.put(("err", e))
            return
        with win_lock:
            won = not stop.is_set()
            if won:
                stop.set()
        if not won:                           # lost the race after connecting
            s.close()
            results.put(("skip", None))
            return
        results.put(("ok", s))

    for i, (fam, sa) in enumerate(addrs):
        threading.Thread(target=attempt, args=(fam, sa, i * _STAGGER),
                         daemon=True).start()

    errors = []
    for _ in range(len(addrs)):
        kind, val = results.get()
        if kind == "ok":
            return val                        # first success wins, immediately
        if kind == "err":
            errors.append(val)
    raise errors[-1] if errors else OSError(f"could not connect to {host}:{port}")


class _HappyEyeballsBackend(SyncBackend):
    def connect_tcp(self, host, port, timeout=None, local_address=None,
                    socket_options=None):
        source = None if local_address is None else (local_address, 0)
        try:
            sock = happy_eyeballs_connect(host, port, timeout, source)
        except socket.timeout as e:
            raise ConnectTimeout(str(e)) from e
        except OSError as e:
            raise ConnectError(str(e)) from e
        for option in socket_options or []:
            sock.setsockopt(*option)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return SyncStream(sock)


class HappyEyeballsTransport(httpx.HTTPTransport):
    """httpx transport that connects with Happy Eyeballs. Accepts the same
    kwargs as HTTPTransport (e.g. `retries=`)."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pool._network_backend = _HappyEyeballsBackend()
