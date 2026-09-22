"""Charter 195 (四): a transport outage pauses the capture; it does not fail it.

2026-09-21 16:04Z the local proxy tunnel stopped carrying traffic for fifteen
hours (charter 197).  Every request of every family failed with a transport
error -- ``ConnectError``, ``PoolTimeout``, TLS EOF -- never an HTTP status,
and each family's own retry loop spent its budget in seconds: ECMWF five tries
backed off at most one second apart, GEFS and ECCC six tries in about fifteen
seconds, ICON three.  So a ten-second hiccup failed a whole family exactly as
the fifteen-hour outage did, and the worker, seeing a failed family, walked the
ladder down to an older run and spent the date's thirty-minute retry floor on
what was never a publication question.

This is a process-level circuit breaker shared by every download thread of a
capture.  After ``BREAKER_THRESHOLD`` consecutive transport errors (any thread,
any host) it opens: every thread about to send a request waits, one of them
sends a single HEAD to that host's root through the same proxy every 30-60
seconds (jittered), and the first HTTP answer of any kind closes it and the
downloads carry on where they were.  Past ``BREAKER_MAX_WAIT_SEC`` it gives up
with a ``ContractError`` whose text starts ``nwp_transport_outage:`` -- the
marker the worker reads to tell "the network is down" from "the run is not
published".  HTTP answers (404, 429, 503) are answers: they reset the count
and keep their own handling in each family, unchanged.
"""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Any, Callable

from .contracts import ContractError


OUTAGE_MARKER = "nwp_transport_outage"
BREAKER_THRESHOLD = int(os.environ.get("PM_NWP_BREAKER_THRESHOLD", "4") or 4)
BREAKER_PAUSE_MIN_SEC = float(os.environ.get("PM_NWP_BREAKER_PAUSE_MIN_SEC", "30") or 30)
BREAKER_PAUSE_MAX_SEC = float(os.environ.get("PM_NWP_BREAKER_PAUSE_MAX_SEC", "60") or 60)
BREAKER_MAX_WAIT_SEC = float(os.environ.get("PM_NWP_BREAKER_MAX_WAIT_SEC", "300") or 300)
PROBE_TIMEOUT_SEC = 10.0
# Transport failures, by class name, across the httpx and stdlib clients the
# families use.  Anything that is an HTTP answer is not here.
TRANSPORT_ERROR_NAMES = frozenset({
    "ConnectError", "ConnectTimeout", "PoolTimeout", "ProxyError",
    "ReadError", "ReadTimeout", "WriteError", "WriteTimeout",
    "RemoteProtocolError", "LocalProtocolError", "NetworkError",
    "TimeoutException", "TransportError", "ProtocolError",
    "URLError", "TimeoutError", "ConnectionError", "ConnectionResetError",
    "ConnectionRefusedError", "ConnectionAbortedError", "SSLError",
    "SSLEOFError", "IncompleteRead", "RemoteDisconnected",
})


def is_transport_error(exc: BaseException) -> bool:
    """An error that says nothing about the object, only about the path."""

    return any(
        klass.__name__ in TRANSPORT_ERROR_NAMES for klass in type(exc).__mro__
    )


def _netloc(url: str) -> str:
    """``scheme://host[:port]/...`` -> ``host[:port]``, without a URL library
    (the dependency audit counts any ``urllib`` import as a network client)."""

    rest = str(url).split("://", 1)[-1]
    return rest.split("/", 1)[0]


def _default_probe(url: str) -> bool:
    """True when the host answers anything at all through the proxy."""

    from .supply_watch import http_head_probe  # noqa: PLC0415
    from official_nwp_https_transport import official_nwp_proxy_url  # noqa: PLC0415

    scheme = str(url).split("://", 1)[0] if "://" in str(url) else "https"
    root = f"{scheme}://{_netloc(url)}/"
    outcome = http_head_probe(root, official_nwp_proxy_url() or "", PROBE_TIMEOUT_SEC)
    return str(outcome.get("outcome") or "") in {"ok", "throttled"}


class TransportBreaker:
    def __init__(
        self,
        *,
        threshold: int = BREAKER_THRESHOLD,
        pause_min_sec: float = BREAKER_PAUSE_MIN_SEC,
        pause_max_sec: float = BREAKER_PAUSE_MAX_SEC,
        max_wait_sec: float = BREAKER_MAX_WAIT_SEC,
        probe: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.pause_min_sec = float(pause_min_sec)
        self.pause_max_sec = max(float(pause_max_sec), float(pause_min_sec))
        self.max_wait_sec = float(max_wait_sec)
        self.probe = probe or _default_probe
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._consecutive = 0
        self._open_since: float | None = None
        self._next_probe: float = 0.0
        self._probing = False
        self._host = ""
        self.trips = 0
        self.paused_sec = 0.0
        self.gave_up = False

    # ------------------------------------------------------------ recording
    def note_transport_error(self, url: str) -> None:
        with self._lock:
            self._consecutive += 1
            if self._open_since is None and self._consecutive >= self.threshold:
                now = self.clock()
                self._open_since = now
                self._next_probe = now + self._pause()
                self._host = _netloc(url)
                self.trips += 1

    def note_answer(self) -> None:
        with self._lock:
            self._consecutive = 0

    def _pause(self) -> float:
        return random.uniform(self.pause_min_sec, self.pause_max_sec)

    # ---------------------------------------------------------------- guard
    def guard(self, url: str) -> None:
        """Wait while the breaker is open; raise once it has waited too long."""

        started = None
        while True:
            with self._lock:
                if self._open_since is None:
                    if started is not None:
                        self.paused_sec += self.clock() - started
                    return
                now = self.clock()
                if started is None:
                    started = now
                if now - self._open_since > self.max_wait_sec:
                    self.gave_up = True
                    raise ContractError(
                        f"{OUTAGE_MARKER}:{self._host}:"
                        f"{now - self._open_since:.0f}s"
                    )
                probe_now = not self._probing and now >= self._next_probe
                if probe_now:
                    self._probing = True
                wait = max(0.2, min(5.0, self._next_probe - now))
            if not probe_now:
                self.sleep(wait)
                continue
            try:
                answered = bool(self.probe(url))
            except Exception:  # noqa: BLE001 -- a failed probe is "still down"
                answered = False
            with self._lock:
                self._probing = False
                if answered:
                    self._open_since = None
                    self._consecutive = 0
                else:
                    self._next_probe = self.clock() + self._pause()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "trips": self.trips,
                "paused_sec": round(self.paused_sec, 1),
                "open": self._open_since is not None,
                "gave_up": self.gave_up,
                "host": self._host,
            }


_BREAKER = TransportBreaker()


def breaker() -> TransportBreaker:
    """The process's breaker, shared by every download thread."""

    return _BREAKER


def reset_for_tests(**kwargs: Any) -> TransportBreaker:
    global _BREAKER
    _BREAKER = TransportBreaker(**kwargs)
    return _BREAKER


def guard(url: str) -> None:
    _BREAKER.guard(url)


def note_transport_error(url: str) -> None:
    _BREAKER.note_transport_error(url)


def note_answer() -> None:
    _BREAKER.note_answer()


__all__ = [
    "OUTAGE_MARKER",
    "TransportBreaker",
    "breaker",
    "guard",
    "is_transport_error",
    "note_answer",
    "note_transport_error",
]
