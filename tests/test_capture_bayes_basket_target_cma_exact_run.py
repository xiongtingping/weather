from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.p0_producer import _current_runs, _curve_rows
import scripts.capture_bayes_basket_target_cma_exact_run as exact_run
from scripts.capture_bayes_basket_target_cma_exact_run import capture


@pytest.fixture(autouse=True)
def _ample_free_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        exact_run.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=64 * 1024**3),
    )


class _Response:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode()
        self.headers = {"Content-Length": str(len(self.body))}
        self.url = "https://single-runs-api.open-meteo.com/test"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, _chunk_size: int):
        yield self.body


class _Session:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}

    def get(self, *_args, **_kwargs):
        return _Response(self.payload)


def _payload(
    *,
    missing_last: bool = False,
    start: datetime | None = None,
) -> dict:
    start = start or datetime(2026, 7, 29, 5, tzinfo=timezone.utc)
    count = 23 if missing_last else 24
    return {
        "hourly": {
            "time": [
                (start + timedelta(hours=index))
                .replace(tzinfo=None)
                .isoformat(timespec="minutes")
                for index in range(count)
            ],
            "temperature_2m": [28.0 + index / 10 for index in range(count)],
        }
    }


def test_cma_exact_run_emits_p0_compatible_aggregator_curve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exact_run, "_now_utc", lambda: "2026-07-28T00:00:00Z")
    output = tmp_path / "decode.json"
    result = capture(
        stations=["MPMG"],
        event_date="2026-07-29",
        run_initialization_utc="2026-07-27T18:00:00Z",
        as_of_utc="2026-07-28T00:00:00Z",
        raw_root=tmp_path / "raw-root",
        output_path=output,
        session=_Session(_payload()),
    )

    assert result["curves"]["complete_curve_count"] == 1
    curve = result["curves"]["curves"][0]
    assert curve["source_class"] == "aggregator_republisher"
    assert curve["model_id"] == "CMA"
    assert curve["model_run_initialization_utc"] == "2026-07-27T18:00:00Z"
    assert all(
        point["published_at_utc"] is None
        and point["received_at_utc"] == "2026-07-28T00:00:00Z"
        for point in curve["points"]
    )
    grouped = _curve_rows(result, event_date="2026-07-29")
    runs = _current_runs(
        grouped["MPMG"][0],
        station="MPMG",
        event_date="2026-07-29",
        lead_bucket="d2",
        run_cycle="18Z",
        season_bin="summer",
        settlement_timezone="America/Panama",
    )
    assert runs[0].source_class == "aggregator_republisher"
    assert runs[0].published_at_utc is None
    assert runs[0].is_asof("2026-07-28T00:00:00Z") is True
    assert output.is_file()


def test_icon_exact_run_uses_explicit_aggregator_profile(tmp_path: Path) -> None:
    session = _Session(_payload())
    result = capture(
        stations=["MPMG"],
        event_date="2026-07-29",
        run_initialization_utc="2026-07-27T18:00:00Z",
        as_of_utc="2026-07-28T00:00:00Z",
        raw_root=tmp_path / "raw-root",
        output_path=tmp_path / "decode.json",
        session=session,
        model_id="ICON",
    )

    curve = result["curves"]["curves"][0]
    assert curve["provider_id"] == "icon_global_open_meteo"
    assert curve["model_id"] == "ICON"
    assert curve["source_class"] == "aggregator_republisher"
    assert result["checks"]["native_publication_not_claimed"] is True
    assert result["checks"]["native_arrival_not_claimed"] is True


def test_jma_msm_exact_run_uses_station_scoped_regional_profile(
    tmp_path: Path,
) -> None:
    result = capture(
        stations=["RKPK"],
        event_date="2026-07-29",
        run_initialization_utc="2026-07-28T12:00:00Z",
        as_of_utc="2026-07-28T13:00:00Z",
        raw_root=tmp_path / "raw-root",
        output_path=tmp_path / "decode.json",
        session=_Session(_payload(
            start=datetime(2026, 7, 28, 15, tzinfo=timezone.utc),
        )),
        model_id="JMA-MSM",
    )
    curve = result["curves"]["curves"][0]
    assert curve["provider_id"] == "jma_msm_open_meteo"
    assert curve["model_id"] == "JMA-MSM"
    assert curve["source_class"] == "aggregator_republisher"


def test_cma_exact_run_rejects_incomplete_settlement_curve(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="curve_incomplete"):
        capture(
            stations=["MPMG"],
            event_date="2026-07-29",
            run_initialization_utc="2026-07-27T18:00:00Z",
            as_of_utc="2026-07-28T00:00:00Z",
            raw_root=tmp_path / "raw-root",
            output_path=tmp_path / "decode.json",
            session=_Session(_payload(missing_last=True)),
        )


def test_cma_exact_run_rejects_future_run(tmp_path: Path) -> None:
    with pytest.raises(
        ContractError,
        match="initialization_after_asof",
    ):
        capture(
            stations=["MPMG"],
            event_date="2026-07-29",
            run_initialization_utc="2026-07-28T18:00:00Z",
            as_of_utc="2026-07-28T00:00:00Z",
            raw_root=tmp_path / "raw-root",
            output_path=tmp_path / "decode.json",
            session=_Session(_payload()),
        )


def test_cma_exact_run_fails_closed_when_disk_reserve_is_low(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        exact_run.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    with pytest.raises(ContractError, match="cma_exact_run_min_free_disk_guard"):
        capture(
            stations=["MPMG"],
            event_date="2026-07-29",
            run_initialization_utc="2026-07-27T18:00:00Z",
            as_of_utc="2026-07-28T00:00:00Z",
            raw_root=tmp_path / "raw-root",
            output_path=tmp_path / "decode.json",
            session=_Session(_payload()),
        )
