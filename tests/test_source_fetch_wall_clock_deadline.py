"""An observation fetch ends on wall clock, whatever its fallbacks.

2026-10-20 05:31-07:28Z: three committed AWC polls fleet-wide in two hours,
MADIS stopped with them (the slow lane runs MADIS, ASOS and AWC in one
round), and ZUUU's first 27 C report reached P1 an hour and a half late.  The
transports' ``timeout`` bounds one socket read; a body trickling in under it,
then each fallback in turn, has no end.
"""

from __future__ import annotations

import time

import pytest

import asos_one_minute_ingress as asos
import reported_trajectory_sources as sources


class _Trickle:
    """A 200 whose body arrives one byte per read, for ever."""

    headers = {"Content-Length": "0"}
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        time.sleep(0.05)
        return b"M"


class _TrickleOpener:
    def open(self, *_args, **_kwargs):
        return _Trickle()


def test_a_trickling_awc_body_ends_the_whole_fetch_on_time(monkeypatch) -> None:
    monkeypatch.setattr(
        sources.urllib.request, "build_opener", lambda *_a, **_k: _TrickleOpener(),
    )
    fallbacks: list[str] = []
    monkeypatch.setattr(
        sources, "_curl_official_get",
        lambda url, **_k: fallbacks.append(url) or b"",
    )
    started = time.monotonic()
    with pytest.raises(sources.SourceFetchDeadlineExceeded):
        sources.fetch_awc_temperature_reports(
            ["ZUUU"], hours=1.0, timeout=8.0, total_timeout=0.6,
        )
    assert time.monotonic() - started < 3.0
    # Spent on the first transport: no curl retry, cache or batch after it.
    assert fallbacks == []


def test_the_deadline_is_per_fetch_and_does_not_leak(monkeypatch) -> None:
    monkeypatch.setattr(
        sources.urllib.request, "build_opener", lambda *_a, **_k: _TrickleOpener(),
    )
    monkeypatch.setattr(sources, "_curl_official_get", lambda url, **_k: b"")
    with pytest.raises(sources.SourceFetchDeadlineExceeded):
        sources.fetch_awc_temperature_reports(
            ["ZUUU"], hours=1.0, timeout=8.0, total_timeout=0.3,
        )
    assert sources._FETCH_DEADLINE.get() is None
    assert sources._within_deadline(5.0) == 5.0


def test_batch_fallback_threads_carry_the_deadline(monkeypatch) -> None:
    seen: list[float | None] = []

    def one(batch, *, hours, timeout, deadline=None):
        seen.append(deadline)
        raise RuntimeError("down")

    class _Refused:
        def open(self, *_a, **_k):
            raise OSError("refused")

    monkeypatch.setattr(
        sources.urllib.request, "build_opener", lambda *_a, **_k: _Refused(),
    )
    monkeypatch.setattr(
        sources, "_curl_official_get",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("curl down")),
    )
    monkeypatch.setattr(sources, "_awc_one", one)
    monkeypatch.setattr(sources, "tgftp_fallback_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="awc_all_transports_failed"):
        sources.fetch_awc_temperature_reports(
            ["ZUUU", "ZBAA"], hours=1.0, timeout=1.0, batch_size=1,
        )
    assert seen and all(isinstance(value, float) for value in seen)


def test_a_trickling_asos_body_ends_on_time(monkeypatch) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asos._read_bounded(_Trickle(), deadline=time.monotonic() + 0.3)
    assert time.monotonic() - started < 2.0
