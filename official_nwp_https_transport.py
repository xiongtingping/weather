"""Certificate-verified HTTPS transport for direct official NWP collectors."""

from __future__ import annotations

import ssl
from typing import Any
from urllib.request import Request, urlopen


def official_https_context() -> ssl.SSLContext:
    """Use certifi roots when a launchd Python lacks a system CA file.

    This is an explicit trust-store selection, not TLS relaxation.  Hostname
    checks and certificate verification remain enabled by
    ``ssl.create_default_context``.
    """

    import certifi

    return ssl.create_default_context(cafile=certifi.where())


def open_official_https(request: Request, *, timeout_sec: float) -> Any:
    return urlopen(request, timeout=max(1.0, float(timeout_sec)), context=official_https_context())


__all__ = ["official_https_context", "open_official_https"]
