"""The network went away: a capture's third outcome (user 2026-10-20 「所有采集车道都应该增加"网络断了"重试」).

A capture pass used to have two answers -- "stored it" and "not out yet" --
and the transport helpers folded a dead connection into the second: every
retry ends in ``httpx.HTTPError``, the helper returns ``None``, the lane reads
``None`` as 404, and the pass ends clean with nothing stored and its whole
interval to wait.  Measured 2026-10-20: ``capture_deterministic_forward`` ran
53 s at 07:11Z with the AIFS Single and HRES 00Z runs both published, stored
neither, and was next due at 10:11Z; the doorbell at 07:31Z, failing the same
way, wrote every product "unpublished".

So a transport failure is its own outcome.  A helper whose retries all end on
the wire raises ``NetworkUnavailable`` (only the source's own "not there"
status is "not published"); a lane that saw one prints
``HOSTED_SWEEP_NETWORK_RETRY_MARKER``; the resident records the pass as
``network_retry`` -- not ``ok``, so the completion stamp keeps saying nothing
was stored -- and brings it due again after a short, doubling delay capped at
the lane's own interval.  A sweep that dies of an uncaught transport error is
read the same way off its output (``output_names_network_failure``).
"""

from __future__ import annotations

import atexit
import re
import socket
import ssl
import sys
import urllib.error

HOSTED_SWEEP_NETWORK_RETRY_MARKER = "hosted_sweep:network_retry"
HOSTED_SWEEP_NETWORK_RETRY_STATE = "network_retry"
# First retry after five minutes, doubling per consecutive network failure,
# never later than the lane's own interval nor than half an hour: after the
# 571-minute proxy outage of 2026-10-31 the doubling had reached each lane's
# 3 h interval, so the captures sat out up to three hours of restored network
# (user 2026-11-01 「封顶 30 分钟」).
NETWORK_RETRY_FIRST_DELAY_SEC = 300.0
NETWORK_RETRY_MAX_DELAY_SEC = 1800.0


class NetworkUnavailable(ConnectionError):
    """Every attempt ended on the wire or on a transient server status --
    the source never answered whether the object exists."""


# Transport exception class names of the libraries the lanes use (httpx,
# requests/urllib3, the stdlib).  Matched by name through the MRO so this
# module imports none of them.
_NETWORK_CLASS_NAMES = frozenset({
    "NetworkUnavailable",
    # httpx
    "TransportError", "TimeoutException", "ConnectError", "ConnectTimeout",
    "ReadTimeout", "WriteTimeout", "PoolTimeout", "ReadError", "WriteError",
    "CloseError", "RemoteProtocolError", "NetworkError", "ProxyError",
    # requests / urllib3
    "ConnectionError", "Timeout", "SSLError", "ProtocolError",
    "NewConnectionError", "NameResolutionError", "MaxRetryError",
    "ChunkedEncodingError", "IncompleteRead",
    # stdlib
    "URLError", "gaierror", "SSLEOFError", "SSLZeroReturnError",
    "TimeoutError", "ConnectionResetError", "ConnectionRefusedError",
    "ConnectionAbortedError", "BrokenPipeError", "RemoteDisconnected",
})
_STDLIB_NETWORK_TYPES = (
    ConnectionError, TimeoutError, ssl.SSLError, socket.gaierror,
    urllib.error.URLError,
)
_TAIL_PATTERN = re.compile(
    r"\b(?:httpx\.|requests\.exceptions\.|urllib3\.exceptions\.|ssl\.|socket\.|urllib\.error\.)?"
    r"(NetworkUnavailable|ConnectError|ConnectTimeout|ReadTimeout|WriteTimeout|PoolTimeout|"
    r"ReadError|RemoteProtocolError|NewConnectionError|NameResolutionError|MaxRetryError|"
    r"SSLError|SSLEOFError|URLError|gaierror|ConnectionResetError|ConnectionRefusedError|"
    r"RemoteDisconnected|ChunkedEncodingError)\b"
)


def is_network_error(exc: BaseException | None) -> bool:
    """Is this failure the wire's, anywhere in its cause chain?"""

    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, _STDLIB_NETWORK_TYPES):
            return True
        if any(cls.__name__ in _NETWORK_CLASS_NAMES for cls in type(exc).__mro__[:-2]):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def output_names_network_failure(text: str) -> bool:
    """Does a dead sweep's output tail end on a transport error?"""

    return bool(_TAIL_PATTERN.search(str(text or "")))


def announce_network_retry(detail: str) -> None:
    """Say the pass lost the network, for the reader and for the resident."""

    print(f"  网络断了，稍后重试：{detail}")
    print(HOSTED_SWEEP_NETWORK_RETRY_MARKER)


_NOTED: list[str] = []


def note_network_failure(detail: str) -> None:
    """Remember that this pass lost the network; the marker is printed once,
    when the process exits, whichever of a lane's exits it takes."""

    if not _NOTED:
        atexit.register(_announce_noted)
    _NOTED.append(str(detail))


def _announce_noted() -> None:
    if _NOTED:
        announce_network_retry(f"{len(_NOTED)} 处，首处 {_NOTED[0]}")
        sys.stdout.flush()


def network_retry_delay(interval_sec: float, streak: int) -> float:
    """How long until a lane whose last ``streak`` passes lost the network
    comes due again."""

    step = NETWORK_RETRY_FIRST_DELAY_SEC * (2 ** max(0, int(streak) - 1))
    return min(float(interval_sec), step, NETWORK_RETRY_MAX_DELAY_SEC)
