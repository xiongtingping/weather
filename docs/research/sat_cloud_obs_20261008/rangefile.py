"""Read-only HTTP Range file object so h5py reads only the bytes it needs.

Public NOAA open-data buckets (no account). Every byte fetched is counted
and capped; blocks are cached in memory for one file only.
"""
from __future__ import annotations

import io
import threading
import time

import certifi
import requests

BLOCK = 16 * 1024  # 64 KB overfetched ~2x on 200x200 Himawari chunks (measured 2026-10-08)
_LOCAL = threading.local()
# Production's market WS listener pulls ~620 KB/s and its NWP capture bursts
# to ~1.5 MB/s on the same uplink (nettop 2026-10-08); research fetches share
# one global budget well under that so neither waits on a satellite file.
RATE_BYTES_PER_S = 400 * 1024
_RATE_LOCK = threading.Lock()
_NEXT = [0.0]


def throttle(nbytes: int) -> None:
    with _RATE_LOCK:
        now = time.monotonic()
        start = max(now, _NEXT[0])
        _NEXT[0] = start + nbytes / RATE_BYTES_PER_S
    if start > now:
        time.sleep(start - now)


def session(route: str = "direct") -> requests.Session:
    """One keep-alive session per thread and route: the TLS handshake costs
    0.5 s direct and ~3 s through the proxy, so it is paid once per worker.
    Direct keeps the bulk off the local tunnel production's Polymarket
    traffic needs; direct connections are sometimes reset by the far end,
    so ``fetch`` falls back to the proxy route."""
    key = "session_" + route
    s = getattr(_LOCAL, key, None)
    if s is None:
        s = requests.Session()
        s.trust_env = route == "proxy"
        s.verify = certifi.where()
        s.headers["User-Agent"] = "weather-research"
        setattr(_LOCAL, key, s)
    return s


def reset(route: str) -> None:
    s = getattr(_LOCAL, "session_" + route, None)
    if s is not None:
        s.close()
        setattr(_LOCAL, "session_" + route, None)


STATS = {"requests": 0, "failures": 0, "seconds": 0.0}


def fetch(url: str, headers: dict | None = None, timeout: float = 60.0) -> requests.Response:
    last = None
    for attempt, route in enumerate(("direct", "direct", "proxy", "proxy", "direct")):
        began = time.monotonic()
        try:
            r = session(route).get(url, headers=headers or {}, timeout=timeout)
            STATS["requests"] += 1
            STATS["seconds"] += time.monotonic() - began
            if r.status_code in (200, 206):
                return r
            last = IOError(f"http_status_{r.status_code}")
        except requests.RequestException as exc:
            STATS["failures"] += 1
            STATS["seconds"] += time.monotonic() - began
            last = exc
            reset(route)
        time.sleep(1.5 * (attempt + 1))
    raise IOError(f"fetch_failed:{last!r}"[:300])


class RangeFile(io.RawIOBase):
    def __init__(self, url: str, size: int, cap_bytes: int = 64 * 1024 ** 2, timeout: float = 60.0):
        self.url, self.size, self.cap, self.timeout = url, size, cap_bytes, timeout
        self.pos = 0
        self.fetched = 0
        self.requests = 0
        self.blocks: dict[int, bytes] = {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def _fetch(self, first: int, last: int) -> None:
        start, end = first * BLOCK, min(self.size, (last + 1) * BLOCK) - 1
        throttle(end - start + 1)
        r = fetch(self.url, {"Range": f"bytes={start}-{end}"}, self.timeout)
        if r.status_code != 206:
            raise IOError(f"range_status_{r.status_code}")
        body = r.content
        if len(body) != end - start + 1:
            raise IOError("range_short_read")
        self.fetched += len(body)
        self.requests += 1
        if self.fetched > self.cap:
            raise IOError("range_fetch_cap_exceeded")
        for i in range(first, last + 1):
            self.blocks[i] = body[(i - first) * BLOCK:(i - first + 1) * BLOCK]

    def readinto(self, b) -> int:
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        first, last = self.pos // BLOCK, (self.pos + n - 1) // BLOCK
        missing = [i for i in range(first, last + 1) if i not in self.blocks]
        if missing:
            self._fetch(missing[0], missing[-1])
        data = b"".join(self.blocks[i] for i in range(first, last + 1))
        off = self.pos - first * BLOCK
        b[:n] = data[off:off + n]
        self.pos += n
        return n
