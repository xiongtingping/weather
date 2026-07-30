from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from bayes_basket_target import ContractError
from scripts.capture_bayes_basket_target_open_meteo_icon import (
    _registry_timezone,
)


def _write_registry(path: Path, rows: list[dict[str, str]]) -> None:
    path.write_text(
        json.dumps({"rows": rows}, sort_keys=True),
        encoding="utf-8",
    )


def test_open_meteo_historical_timezone_carries_invariant_registry_backward(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry.json"
    _write_registry(
        registry,
        [
            {
                "station_id": station,
                "event_date": "2026-07-28",
                "settlement_timezone": "Asia/Shanghai",
            }
            for station in ("ZBAA", "ZUUU")
        ],
    )

    assert _registry_timezone(
        registry,
        stations=["ZBAA", "ZUUU"],
        event_dates=[date(2026, 7, 14), date(2026, 7, 15)],
    ) == "Asia/Shanghai"


def test_open_meteo_historical_timezone_rejects_mixed_registry_scope(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry.json"
    _write_registry(
        registry,
        [
            {
                "station_id": "ZBAA",
                "event_date": "2026-07-28",
                "settlement_timezone": "Asia/Shanghai",
            },
            {
                "station_id": "RCSS",
                "event_date": "2026-07-28",
                "settlement_timezone": "Asia/Taipei",
            },
        ],
    )

    with pytest.raises(
        ContractError,
        match="market_registry_mixed_settlement_timezones_forbidden",
    ):
        _registry_timezone(
            registry,
            stations=["ZBAA", "RCSS"],
            event_dates=[date(2026, 7, 14)],
        )
