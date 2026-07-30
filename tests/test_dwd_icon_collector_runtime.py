from __future__ import annotations

from http.client import IncompleteRead

from scripts import collect_dwd_icon_zuuu as collector


def test_icon_parallel_fetch_preserves_direct_receipt_order(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout_sec: float):
        calls.append(url)
        return url.encode("utf-8"), {"requested_at": url, "response_started_at": url, "response_completed_at": url, "response_headers_observed": True}

    monkeypatch.setattr(collector, "_fetch", fake_fetch)
    urls = ["https://dwd.example/first", "https://dwd.example/second", "https://dwd.example/third"]
    rows = collector._fetch_icon_payloads(urls, timeout_sec=1.0)

    assert [row[0] for row in rows] == urls
    assert [row[1] for row in rows] == [url.encode("utf-8") for url in urls]
    assert {row[2]["requested_at"] for row in rows} == set(urls)
    assert set(calls) == set(urls)


def test_icon_cdo_resolves_homebrew_binary_when_launchd_path_is_minimal(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "cdo"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(collector.shutil, "which", lambda _name: None)
    monkeypatch.setattr(collector, "Path", lambda value: type(binary)(str(binary)) if value == "/opt/homebrew/bin/cdo" else type(binary)(value))

    assert collector._cdo_binary() == str(binary)


def test_icon_fetch_retries_incomplete_body_and_receipts_only_successful_attempt(monkeypatch) -> None:
    timestamps = iter([
        "2026-07-17T03:00:00.000Z",  # failed attempt request
        "2026-07-17T03:00:01.000Z",  # failed attempt headers
        "2026-07-17T03:00:02.000Z",  # successful attempt request
        "2026-07-17T03:00:03.000Z",  # successful attempt headers
        "2026-07-17T03:00:04.000Z",  # successful attempt complete
    ])
    reads = iter([IncompleteRead(b"partial"), b"complete"])
    sleeps: list[float] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr(collector, "_now", lambda: next(timestamps))
    monkeypatch.setattr(collector, "open_official_https", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(collector.time, "sleep", sleeps.append)

    body, receipt = collector._fetch("https://opendata.dwd.de/object", timeout_sec=1.0)

    assert body == b"complete"
    assert receipt == {
        "requested_at": "2026-07-17T03:00:02.000Z",
        "response_started_at": "2026-07-17T03:00:03.000Z",
        "response_completed_at": "2026-07-17T03:00:04.000Z",
        "response_headers_observed": True,
        "transport_attempt": 2,
    }
    assert sleeps == [1.0]
