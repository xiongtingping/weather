"""Certificate-verified HTTPS transport for direct official NWP collectors."""

from __future__ import annotations

import json
import errno
from pathlib import Path
import signal
import ssl
import threading
import time
from contextlib import contextmanager
from typing import Any
from urllib.error import URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


OFFICIAL_NWP_PROXY_ENV = "PM_OFFICIAL_NWP_PROXY"
OFFICIAL_NWP_NETWORK_CONFIG = (
    Path(__file__).resolve().parent
    / "config"
    / "trading"
    / "bayes_basket_target_research_network_v1.json"
)
OFFICIAL_NWP_NETWORK_SCHEMA = "bayes_basket_target_research_network_v1"
MAX_OFFICIAL_NWP_NETWORK_CONFIG_BYTES = 16 * 1024


@contextmanager
def response_read_deadline(timeout_sec: float):
    """Enforce a hard wall-clock bound around an uncooperative ``read``."""

    seconds = max(0.01, float(timeout_sec))
    installed = False
    previous_handler = None
    previous_timer = (0.0, 0.0)
    started = time.monotonic()
    if (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "ITIMER_REAL")
        and threading.current_thread() is threading.main_thread()
    ):
        try:
            previous_handler = signal.getsignal(signal.SIGALRM)
            previous_timer = signal.setitimer(signal.ITIMER_REAL, 0.0)

            def _raise_timeout(_signum: int, _frame: Any) -> None:
                raise TimeoutError("official_https_response_wall_clock_timeout")

            signal.signal(signal.SIGALRM, _raise_timeout)
            signal.setitimer(signal.ITIMER_REAL, seconds)
            installed = True
        except (OSError, TypeError, ValueError):
            if previous_timer[0] > 0.0:
                try:
                    signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
                except (OSError, TypeError, ValueError):
                    pass
    try:
        yield
    finally:
        if installed:
            elapsed = max(0.0, time.monotonic() - started)
            remaining = max(0.0, previous_timer[0] - elapsed)
            try:
                signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])
                signal.signal(signal.SIGALRM, previous_handler)
            except (OSError, TypeError, ValueError):
                pass


def _validated_proxy_url(value: Any, *, source: str) -> str:
    from urllib.parse import urlparse

    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{source} must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{source} must not contain credentials, query, or fragment")
    return candidate


def _proxy_from_network_config(path: Path) -> str:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ValueError("official_nwp_network_config_unreadable") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError("official_nwp_network_config_regular_file_required")
    if stat.st_size > MAX_OFFICIAL_NWP_NETWORK_CONFIG_BYTES:
        raise ValueError("official_nwp_network_config_too_large")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("official_nwp_network_config_unreadable") from exc
    if len(raw) > MAX_OFFICIAL_NWP_NETWORK_CONFIG_BYTES:
        raise ValueError("official_nwp_network_config_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("official_nwp_network_config_invalid_json") from exc
    if not isinstance(value, dict) or value.get("schema") != OFFICIAL_NWP_NETWORK_SCHEMA:
        raise ValueError("official_nwp_network_config_schema_invalid")
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("official_nwp_network_config_enabled_boolean_required")
    if not enabled:
        return ""
    return _validated_proxy_url(
        value.get("proxy_url"),
        source="official_nwp_network_config.proxy_url",
    )


def official_nwp_proxy_url() -> str:
    """Return the explicitly configured official-NWP proxy, if any.

    Official-source collectors deliberately do not honor the ambient
    ``HTTP(S)_PROXY`` environment: that environment is also populated by
    desktop tools and can silently route a provider through the wrong tunnel.
    The production transport therefore has one explicit, auditable knob.
    """

    import os

    value = str(os.environ.get(OFFICIAL_NWP_PROXY_ENV) or "").strip()
    if value:
        return _validated_proxy_url(value, source=OFFICIAL_NWP_PROXY_ENV)
    return _proxy_from_network_config(OFFICIAL_NWP_NETWORK_CONFIG)


def configure_official_requests_session(session: Any, *, proxy_url: str | None = None) -> Any:
    """Make a requests session use only the explicit official-NWP proxy.

    ``proxy_url=None`` reads the configured explicit proxy. Passing an empty
    string is the deliberate, bounded direct-transport fallback used after a
    proxy connection failure; it never consults ambient proxy variables.
    """

    session.trust_env = False
    proxy = official_nwp_proxy_url() if proxy_url is None else str(proxy_url).strip()
    session.proxies.clear()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def official_nwp_proxy_handler(
    proxy_url: str | None = None,
) -> ProxyHandler:
    proxy = (
        official_nwp_proxy_url()
        if proxy_url is None
        else str(proxy_url).strip()
    )
    return ProxyHandler({"http": proxy, "https": proxy} if proxy else {})


def official_https_context() -> ssl.SSLContext:
    """Use certifi roots when a launchd Python lacks a system CA file.

    This is an explicit trust-store selection, not TLS relaxation.  Hostname
    checks and certificate verification remain enabled by
    ``ssl.create_default_context``.
    """

    import certifi

    return ssl.create_default_context(cafile=certifi.where())


def open_official_https(
    request: Request,
    *,
    timeout_sec: float,
    proxy_url: str | None = None,
) -> Any:
    # Keep TLS verification and hostname checking enabled while making the
    # proxy boundary explicit and bounded.  With no explicit proxy this is a
    # direct connection; ambient desktop proxy variables are never used.
    selected_proxy = (
        official_nwp_proxy_url()
        if proxy_url is None
        else str(proxy_url).strip()
    )
    opener = build_opener(
        official_nwp_proxy_handler(selected_proxy),
        HTTPSHandler(context=official_https_context()),
    )
    timeout = max(1.0, float(timeout_sec))
    try:
        return opener.open(request, timeout=timeout)
    except URLError as exc:
        # A stale local proxy configuration must not make an otherwise
        # reachable official provider unavailable.  Retry direct only for an
        # explicit TCP connection-refused failure at the proxy.  DNS, TLS,
        # timeout, provider HTTP and payload failures stay fail closed.
        reason: Any = exc.reason
        connection_refused = isinstance(reason, ConnectionRefusedError)
        while isinstance(reason, OSError) and not connection_refused:
            connection_refused = getattr(reason, "errno", None) == errno.ECONNREFUSED
            nested = getattr(reason, "reason", None)
            if nested is None or nested is reason:
                break
            reason = nested
        if not selected_proxy or not connection_refused:
            raise
        # ``ProxyHandler`` mutates the Request with the proxy host.  Reusing
        # that object would make the nominal direct retry dial the refused
        # local proxy a second time.  Rebuild the request from its original
        # URL and public headers; never forward proxy authorization.
        direct_headers = {
            key: value
            for key, value in request.header_items()
            if key.lower() not in {"proxy-authorization", "proxy-connection"}
        }
        direct_request = Request(
            request.full_url,
            data=request.data,
            headers=direct_headers,
            origin_req_host=request.origin_req_host,
            unverifiable=request.unverifiable,
            method=request.get_method(),
        )
        direct = build_opener(
            official_nwp_proxy_handler(""),
            HTTPSHandler(context=official_https_context()),
        )
        return direct.open(direct_request, timeout=timeout)


def read_official_response(response: Any, *, timeout_sec: float, chunk_size: int = 1024 * 1024) -> bytes:
    """Read an official response with both idle and total wall-clock bounds.

    ``urllib``'s socket timeout is only an idle-read timeout.  A proxy that
    drips bytes forever can therefore keep ``response.read()`` alive for
    hours, leaving the source scheduler with an apparently running but
    unusable collector.  Chunked reads make the total deadline enforceable
    even when the provider keeps the connection open.
    """

    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    chunks: list[bytes] = []
    with response_read_deadline(timeout_sec):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise TimeoutError("official_https_response_wall_clock_timeout")
            raw_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if raw_socket is not None:
                try:
                    raw_socket.settimeout(max(0.1, min(float(timeout_sec), remaining)))
                except (AttributeError, OSError, TypeError, ValueError):
                    pass
            try:
                block = response.read(max(1, int(chunk_size)))
            except TypeError:
                # Keep lightweight test doubles and older response wrappers
                # compatible; real urllib responses accept the size argument.
                block = response.read()
                return bytes(block or b"")
            if not block:
                return b"".join(chunks)
            chunks.append(bytes(block))


__all__ = [
    "MAX_OFFICIAL_NWP_NETWORK_CONFIG_BYTES",
    "OFFICIAL_NWP_NETWORK_CONFIG",
    "OFFICIAL_NWP_NETWORK_SCHEMA",
    "OFFICIAL_NWP_PROXY_ENV",
    "configure_official_requests_session",
    "official_https_context",
    "official_nwp_proxy_handler",
    "official_nwp_proxy_url",
    "open_official_https",
    "response_read_deadline",
    "read_official_response",
]
