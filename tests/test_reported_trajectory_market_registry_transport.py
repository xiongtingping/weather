from __future__ import annotations

from datetime import date

import pytest

import reported_trajectory_market_registry as registry


def test_gamma_keeps_successful_pages_when_one_page_fails(monkeypatch) -> None:
    def fake_json(url: str, *, timeout: float):
        if "offset=100" in url:
            raise registry.GammaFetchError("page_down", errors=("test_page",))
        return [{"slug": "highest-temperature-in-austin-on-july-24-2026"}]

    monkeypatch.setattr(registry, "_gamma_json", fake_json)
    rows = registry.fetch_gamma_events_window(
        start_date=date(2026, 7, 24), end_date=date(2026, 7, 24), timeout=1, max_pages=2
    )
    assert [row["slug"] for row in rows] == [
        "highest-temperature-in-austin-on-july-24-2026"
    ]


def test_gamma_reports_total_transport_failure(monkeypatch) -> None:
    def fail(_url: str, *, timeout: float):
        raise registry.GammaFetchError("offline", errors=("test_transport",))

    monkeypatch.setattr(registry, "_gamma_json", fail)
    with pytest.raises(registry.GammaFetchError):
        registry.fetch_gamma_events_window(
            start_date=date(2026, 7, 24), end_date=date(2026, 7, 24), timeout=1, max_pages=2
        )
