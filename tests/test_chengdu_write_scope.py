import os

import pytest

from ui import server


def test_fact_write_scope_allows_only_chengdu_zuuu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_ARCHIVE_WRITE_SCOPE", "chengdu_zuuu")

    assert server._fact_write_allowed_for_chengdu_zuuu(
        "highest-temperature-in-chengdu-on-june-9-2026",
        {"station_id": "ZUUU", "city": "Chengdu"},
    )
    assert server._fact_write_allowed_for_chengdu_zuuu(
        "some-slug",
        {"resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"},
    )
    assert not server._fact_write_allowed_for_chengdu_zuuu(
        "highest-temperature-in-london-on-june-9-2026",
        {"station_id": "EGLC", "city": "London", "lat": 51.505, "lon": 0.055},
    )


def test_fact_write_scope_defaults_to_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_ARCHIVE_WRITE_SCOPE", raising=False)

    assert server._fact_write_allowed_for_chengdu_zuuu(
        "highest-temperature-in-london-on-june-9-2026",
        {"station_id": "EGLC", "city": "London"},
    )
