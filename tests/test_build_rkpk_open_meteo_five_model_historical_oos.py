from __future__ import annotations

import json
from datetime import date, datetime, time as wall_time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import requests

from bayes_basket_target.contracts import ContractError, content_sha256
from scripts import build_rkpk_open_meteo_five_model_historical_oos as target


class _Response:
    def __init__(self, payload, url):
        self.raw = json.dumps(payload).encode()
        self.url = url
        self.headers = {"Content-Length": str(len(self.raw))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        del chunk_size
        yield self.raw


class _Session:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, endpoint, *, params, timeout, stream):
        model = next(
            key for key, item in target.MODELS.items()
            if item["api_model"] == params["models"]
        )
        self.calls.append((endpoint, dict(params), timeout, stream))
        return _Response(self.payloads[model], endpoint + "?bounded=true")


class _FlakySession(_Session):
    def __init__(self, payloads):
        super().__init__(payloads)
        self.failed_once = False

    def get(self, endpoint, *, params, timeout, stream):
        if not self.failed_once:
            self.failed_once = True
            raise requests.ConnectionError("transient")
        return super().get(
            endpoint, params=params, timeout=timeout, stream=stream
        )


def _window(day):
    zone = ZoneInfo("Asia/Seoul")
    start = datetime.combine(day, wall_time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    end = datetime.combine(
        day + timedelta(days=1), wall_time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    return start, end


def _fixture(count=30):
    first = date(2026, 6, 1)
    samples = []
    all_times = []
    for offset in range(count):
        day = first + timedelta(days=offset)
        start, end = _window(day)
        observations = [{
            "observed_at_utc": start.isoformat().replace("+00:00", "Z"),
            "temperature_c": 30.0 + (offset % 4),
            "report_type": "METAR",
            "raw_report": f"METAR-{day}",
        }]
        samples.append({
            "sample_id": f"truth-{day}",
            "event_date": day.isoformat(),
            "settlement_window": {
                "start_utc": start.isoformat().replace("+00:00", "Z"),
                "end_utc": end.isoformat().replace("+00:00", "Z"),
            },
            "truth": {
                "source_lane": "AWC",
                "tmax_c": 30.0 + (offset % 4),
                "observations": observations,
            },
        })
        cursor = start
        while cursor < end:
            all_times.append(cursor)
            cursor += timedelta(hours=1)
    truth_identity = {
        "schema": "bayes_basket_awc_taf_calibration_dataset_v1",
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "samples": samples,
    }
    truth = {
        **truth_identity,
        "content_sha256": content_sha256(truth_identity),
    }
    variable = "temperature_2m_previous_day2"
    payloads = {}
    for model_index, model in enumerate(target.MODEL_IDS):
        payloads[model] = {
            "latitude": 35.2,
            "longitude": 129.0,
            "elevation": 3.0,
            "hourly": {
                "time": [
                    stamp.replace(tzinfo=None).isoformat(timespec="minutes")
                    for stamp in all_times
                ],
                variable: [
                    29.0 + (index % 4) + model_index * 0.1
                    for index in range(len(all_times))
                ],
            },
        }
    return truth, payloads, first, first + timedelta(days=count - 1)


def test_builds_31_candidates_and_never_activates(monkeypatch):
    truth, payloads, first, last = _fixture()
    monkeypatch.setattr(
        target, "assert_process_memory_budget", lambda **_: None
    )
    result = target.build(
        truth_payload=truth,
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        lead_days=2,
        timeout_seconds=1.0,
        max_response_bytes=1024 * 1024,
        max_total_response_bytes=8 * 1024 * 1024,
        session=_Session(payloads),
        fetched_at_utc="2026-07-01T00:00:00Z",
    )

    assert result["oos"]["state"] == (
        "OOS_RESEARCH_COMPLETE_ACTIVATION_BLOCKED"
    )
    assert result["oos"]["common_sample_n"] == 30
    assert len(result["oos"]["candidate_scores"]) == 31
    assert result["oos"]["research_winner"]["selected_models"]
    assert result["authority"]["formal_calibration_activation_eligible"] is False
    assert result["authority"]["p0_activation_eligible"] is False
    assert len(result["captures"]) == 5


def test_insufficient_closed_days_produces_blocker(monkeypatch):
    truth, payloads, first, last = _fixture(count=10)
    monkeypatch.setattr(
        target, "assert_process_memory_budget", lambda **_: None
    )
    result = target.build(
        truth_payload=truth,
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        lead_days=2,
        timeout_seconds=1.0,
        max_response_bytes=1024 * 1024,
        max_total_response_bytes=8 * 1024 * 1024,
        session=_Session(payloads),
        fetched_at_utc="2026-07-01T00:00:00Z",
    )

    assert result["oos"]["state"] == "BLOCKED_INSUFFICIENT_EVIDENCE"
    assert result["oos"]["candidate_scores"] == []
    assert result["authority"]["p0_activation_eligible"] is False


def test_limited_sample_oos_is_shrunk_from_single_model_chasing(monkeypatch):
    truth, payloads, first, last = _fixture(count=28)
    monkeypatch.setattr(
        target, "assert_process_memory_budget", lambda **_: None
    )
    result = target.build(
        truth_payload=truth,
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        lead_days=2,
        timeout_seconds=1.0,
        max_response_bytes=1024 * 1024,
        max_total_response_bytes=8 * 1024 * 1024,
        session=_Session(payloads),
        fetched_at_utc="2026-07-01T00:00:00Z",
    )

    assert result["oos"]["evidence_tier"] == "LIMITED_SAMPLE_SHRUNK"
    assert result["oos"]["state"] == (
        "OOS_RESEARCH_COMPLETE_ACTIVATION_BLOCKED"
    )
    winner = result["oos"]["research_winner"]
    assert len(winner["selected_models"]) >= 2
    assert max(winner["selected_weights"].values()) <= 0.7 + 1e-12


def test_resource_budget_fails_closed():
    truth, payloads, first, last = _fixture()
    with pytest.raises(
        ContractError,
        match="response_budget_invalid",
    ):
        target.build(
            truth_payload=truth,
            start_date=first.isoformat(),
            end_date=last.isoformat(),
            lead_days=2,
            timeout_seconds=1.0,
            max_response_bytes=target.MAX_RESPONSE_BYTES + 1,
            max_total_response_bytes=target.MAX_TOTAL_RESPONSE_BYTES,
            session=_Session(payloads),
        )


def test_generic_station_uses_registry_scope_and_hemisphere_season(monkeypatch):
    truth, payloads, first, last = _fixture()
    truth["station_id"] = "FACT"
    truth["settlement_timezone"] = "Africa/Johannesburg"
    zone = ZoneInfo("Africa/Johannesburg")
    for sample in truth["samples"]:
        day = date.fromisoformat(sample["event_date"])
        start = datetime.combine(day, wall_time.min, tzinfo=zone).astimezone(
            timezone.utc
        )
        end = datetime.combine(
            day + timedelta(days=1), wall_time.min, tzinfo=zone
        ).astimezone(timezone.utc)
        sample["settlement_window"] = {
            "start_utc": start.isoformat().replace("+00:00", "Z"),
            "end_utc": end.isoformat().replace("+00:00", "Z"),
        }
    truth["content_sha256"] = content_sha256({
        key: value for key, value in truth.items() if key != "content_sha256"
    })
    monkeypatch.setattr(
        target, "assert_process_memory_budget", lambda **_: None
    )
    result = target.build(
        truth_payload=truth,
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        lead_days=2,
        timeout_seconds=1.0,
        max_response_bytes=1024 * 1024,
        max_total_response_bytes=8 * 1024 * 1024,
        session=_Session(payloads),
        fetched_at_utc="2026-07-01T00:00:00Z",
        station_id="FACT",
        settlement_timezone="Africa/Johannesburg",
        latitude=-33.965,
        longitude=18.602,
        truth_source_lane="AWC",
        season_bin=None,
    )

    assert result["station_id"] == "FACT"
    assert result["settlement_timezone"] == "Africa/Johannesburg"
    assert result["season_bin"] == "winter"


def test_transient_transport_retries_with_bounded_backoff(monkeypatch):
    truth, payloads, first, last = _fixture()
    monkeypatch.setattr(
        target, "assert_process_memory_budget", lambda **_: None
    )
    sleeps = []
    monkeypatch.setattr(target.time, "sleep", sleeps.append)
    session = _FlakySession(payloads)

    result = target.build(
        truth_payload=truth,
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        lead_days=2,
        timeout_seconds=1.0,
        max_response_bytes=1024 * 1024,
        max_total_response_bytes=8 * 1024 * 1024,
        session=session,
        fetched_at_utc="2026-07-01T00:00:00Z",
    )

    assert result["oos"]["common_sample_n"] == 30
    assert sleeps == [1.0]


def _native_evidence(path, *, event_date, run_cycle="18Z"):
    run = datetime.combine(
        event_date - timedelta(days=2),
        wall_time(hour=18),
        tzinfo=timezone.utc,
    )
    identity = {
        "schema": target.NATIVE_REBUILD_SCHEMA,
        "provider_id": "ecmwf_ifs025_official",
        "run_cycle": run_cycle,
        "run_day_offset": 2,
        "checks": {
            "official_provider_only": True,
            "no_open_meteo": True,
            "calls_order_gateway": False,
            "places_orders": False,
        },
        "blockers": [],
        "model_rows": [{
            "station_id": "RKPK",
            "event_date": event_date.isoformat(),
            "provider_id": "ecmwf_ifs025_official",
            "target_timezone": "Asia/Seoul",
            "daily_curve_complete": True,
            "sample_count": 5,
            "forecast_daily_max_c": 42.0,
            "model_run_initialization_utc":
                run.isoformat().replace("+00:00", "Z"),
            "model_run_lineage_sha256": "a" * 64,
            "receipt_sha256": "b" * 64,
        }],
    }
    payload = {
        **identity,
        "receipt_sha256": content_sha256(identity),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_native_overlay_replaces_aggregator_day_in_exact_join(
    tmp_path,
    monkeypatch,
):
    truth, payloads, first, last = _fixture()
    evidence = _native_evidence(tmp_path / "native.json", event_date=first)
    monkeypatch.setattr(
        target, "assert_process_memory_budget", lambda **_: None
    )

    result = target.build(
        truth_payload=truth,
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        lead_days=2,
        timeout_seconds=1.0,
        max_response_bytes=1024 * 1024,
        max_total_response_bytes=8 * 1024 * 1024,
        session=_Session(payloads),
        fetched_at_utc="2026-07-01T00:00:00Z",
        native_ecmwf_evidence=[evidence],
    )

    joined = next(
        row for row in result["exact_join"]["rows"]
        if row["event_date"] == first.isoformat()
    )
    ecmwf = joined["models"]["ECMWF"]
    assert ecmwf["forecast_tmax_c"] == 42.0
    assert ecmwf["source_class"] == (
        "official_native_historical_rebuild"
    )
    assert ecmwf["availability_class"] == (
        "historical_publication_only"
    )
    assert result["authority"][
        "formal_oos_selection_contract_valid"
    ] is True


def test_native_overlay_rejects_wrong_run_cycle(
    tmp_path,
    monkeypatch,
):
    truth, payloads, first, last = _fixture()
    evidence = _native_evidence(
        tmp_path / "native.json",
        event_date=first,
        run_cycle="00Z",
    )
    monkeypatch.setattr(
        target, "assert_process_memory_budget", lambda **_: None
    )

    with pytest.raises(
        ContractError,
        match="native_overlay_evidence_invalid",
    ):
        target.build(
            truth_payload=truth,
            start_date=first.isoformat(),
            end_date=last.isoformat(),
            lead_days=2,
            timeout_seconds=1.0,
            max_response_bytes=1024 * 1024,
            max_total_response_bytes=8 * 1024 * 1024,
            session=_Session(payloads),
            fetched_at_utc="2026-07-01T00:00:00Z",
            native_ecmwf_evidence=[evidence],
        )
