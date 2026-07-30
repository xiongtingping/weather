from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.resource_bounds import load_bounded_json
from bayes_basket_target.station_registry import coordinate_registry_manifest
import scripts.rebuild_bayes_basket_target_historical_regional as regional_rebuild
from scripts.rebuild_bayes_basket_target_historical_regional import rebuild


@pytest.fixture(autouse=True)
def _ample_free_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        regional_rebuild.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=64 * 1024**3),
    )


class _Response:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode()
        self.headers = {"Content-Length": str(len(self.body))}
        self.url = "https://single-runs-api.open-meteo.com/test"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, _size: int):
        yield self.body


class _Session:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def get(self, *_args, **_kwargs):
        return _Response(self.payload)


def _hourly() -> dict:
    start = datetime(2026, 6, 29, 12, tzinfo=timezone.utc)
    return {
        "hourly": {
            "time": [
                (start + timedelta(hours=index))
                .replace(tzinfo=None)
                .isoformat(timespec="minutes")
                for index in range(96)
            ],
            "temperature_2m": [20.0 + index / 100 for index in range(96)],
        }
    }


def _scope(tmp_path: Path) -> tuple[Path, Path]:
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({
        "schema": "bayes_basket_target_station_regional_model_pool_v1",
        "global_model_pool": ["ECMWF", "GFS", "ICON", "GEM", "CMA"],
        "global_aggregator_capture_models": {},
        "regional_models": {
            "TEST-REGIONAL": {
                "provider_id": "test_regional_open_meteo",
                "model_parameter": "test_regional",
                "source_class": "aggregator_republisher",
                "run_cycle": "12Z",
                "run_day_offset": 2,
            }
        },
        "station_regional_pool": {"MPMG": ["TEST-REGIONAL"]},
    }))
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "rows": [{
            "station_id": "MPMG",
            "settlement_timezone": "America/Panama",
        }]
    }))
    return pool, registry


def test_regional_pool_covers_exact_canonical_45_station_scope() -> None:
    payload = load_bounded_json(
        Path("config/bayes_basket_target/station_regional_model_pool_v1.json")
    )
    assert set(payload["station_regional_pool"]) == set(
        coordinate_registry_manifest()["station_ids"]
    )
    assert payload["selection_contract"]["minimum_selected_models"] == 2
    assert payload["selection_contract"]["future_targets_for_training_forbidden"] is True


def test_regional_history_rebuild_uses_only_closed_historical_exact_run(
    tmp_path: Path,
) -> None:
    pool, registry = _scope(tmp_path)
    output = tmp_path / "regional.json"
    result = rebuild(
        model_id="TEST-REGIONAL",
        first_date="2026-07-01",
        last_date="2026-07-01",
        as_of_utc="2026-07-03T00:00:00Z",
        pool_path=pool,
        registry_path=registry,
        raw_root=tmp_path / "raw",
        output_path=output,
        session=_Session(_hourly()),
    )
    assert result["blockers"] == []
    assert result["checks"]["future_data_used"] is False
    assert result["model_rows"][0]["model_run_initialization_utc"] == (
        "2026-06-29T12:00:00Z"
    )
    assert result["model_rows"][0]["sample_count"] == 24
    assert output.is_file()


def test_regional_history_rejects_unclosed_target(tmp_path: Path) -> None:
    pool, registry = _scope(tmp_path)
    with pytest.raises(ContractError, match="unclosed_target_forbidden"):
        rebuild(
            model_id="TEST-REGIONAL",
            first_date="2026-07-03",
            last_date="2026-07-03",
            as_of_utc="2026-07-03T12:00:00Z",
            pool_path=pool,
            registry_path=registry,
            raw_root=tmp_path / "raw",
            output_path=tmp_path / "regional.json",
            session=_Session(_hourly()),
        )


def test_regional_history_fails_closed_when_disk_reserve_is_low(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        regional_rebuild.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    pool, registry = _scope(tmp_path)
    result = rebuild(
        model_id="TEST-REGIONAL",
        first_date="2026-07-01",
        last_date="2026-07-01",
        as_of_utc="2026-07-03T00:00:00Z",
        pool_path=pool,
        registry_path=registry,
        raw_root=tmp_path / "raw",
        output_path=tmp_path / "regional.json",
        session=_Session(_hourly()),
    )
    assert result["blockers"] == [{
        "event_date": "2026-07-01",
        "reason": "regional_history_minimum_free_disk_guard",
    }]
