from __future__ import annotations

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
