from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from scripts import capture_bayes_basket_target_open_meteo_icon as icon
from scripts import rebuild_bayes_basket_target_historical_ecmwf as ecmwf
from scripts import rebuild_bayes_basket_target_historical_native as gfs
from scripts import run_bayes_basket_target_45station_explicit_18z_oos as explicit_oos


def test_explicit_oos_residual_matches_formal_p0_additive_bias_contract() -> None:
    residual = explicit_oos._calibration_residual_c(
        forecast_tmax_c=31.5,
        official_tmax_c=30.0,
    )
    assert residual == -1.5
    assert 31.5 + residual == 30.0


def test_gfs_18z_d2_fallback_preserves_real_cycle_and_lead() -> None:
    run = gfs._run_initialization(
        date(2026, 7, 14),
        run_cycle="18Z",
        run_day_offset=2,
    )
    assert run == datetime(2026, 7, 12, 18, tzinfo=timezone.utc)
    assert gfs._gfs_url(run, 34).endswith(
        "/gfs.20260712/18/atmos/gfs.t18z.pgrb2.0p25.f034"
    )


def test_gfs_f000_accepts_official_analysis_index_label() -> None:
    start, end, line = gfs._idx_range(
        (
            b"1:0:d=2026062712:TMP:2 m above ground:anl:\n"
            b"2:100:d=2026062712:RH:2 m above ground:anl:\n"
        ),
        lead_hours=0,
    )
    assert (start, end) == (0, 99)
    assert ":TMP:2 m above ground:anl:" in line


def test_gfs_nonzero_lead_rejects_analysis_index_label() -> None:
    with pytest.raises(
        ContractError,
        match="2m_temperature_index_missing",
    ):
        gfs._idx_range(
            (
                b"1:0:d=2026062712:TMP:2 m above ground:anl:\n"
                b"2:100:d=2026062712:RH:2 m above ground:anl:\n"
            ),
            lead_hours=1,
        )


def test_gfs_message_rejects_truncated_partial_content(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gfs,
        "_fetch",
        lambda url, **kwargs: (
            (
                b"1:0:d=2026062618:TMP:2 m above ground:45 hour fcst:\n"
                b"2:100:d=2026062618:RH:2 m above ground:45 hour fcst:\n"
            ),
            {"Last-Modified": "Sat, 27 Jun 2026 00:00:00 GMT"},
            200,
        )
        if url.endswith(".idx")
        else (
            b"GRIB" + b"x" * 20,
            {"Last-Modified": "Sat, 27 Jun 2026 00:00:00 GMT"},
            206,
        ),
    )

    with pytest.raises(
        ContractError,
        match="range_not_honored",
    ):
        gfs._message(
            raw_root=tmp_path,
            run_initialization=datetime(
                2026, 6, 26, 18, tzinfo=timezone.utc
            ),
            event_date="2026-06-28",
            valid_at=datetime(
                2026, 6, 28, 15, tzinfo=timezone.utc
            ),
            lead_hours=45,
            reconstructed_at="2026-06-30T00:00:00Z",
        )


def test_gfs_mixed_timezone_batch_binds_each_station_local_day(
    tmp_path,
    monkeypatch,
) -> None:
    message_times = {}

    def fake_message(
        *,
        raw_root,
        run_initialization,
        event_date,
        valid_at,
        lead_hours,
        reconstructed_at,
    ):
        del raw_root, lead_hours
        raw_path = tmp_path / (
            valid_at.isoformat().replace(":", "").replace("+", "_")
            + ".grib2"
        )
        message_times[str(raw_path)] = (run_initialization, valid_at)
        token = str(len(message_times)).zfill(64)
        record = {
            "provider_id": gfs.GFS_PROVIDER,
            "event_date": event_date,
            "run_initialization_utc":
                run_initialization.isoformat().replace("+00:00", "Z"),
            "valid_time_utc":
                valid_at.isoformat().replace("+00:00", "Z"),
            "source_url": "https://example.test/gfs",
            "raw_message_sha256": token,
            "receipt_sha256": token[::-1],
            "raw_object_ref": str(raw_path),
            "published_at_utc": reconstructed_at,
            "reconstructed_at_utc": reconstructed_at,
            "production_calibration_eligible": False,
        }
        return record, raw_path

    def fake_decode(raw_path, *, provider_id, station_id):
        run, valid = message_times[str(raw_path)]
        return {
            "points": [{
                "grib_valid_time_utc":
                    valid.isoformat().replace("+00:00", "Z"),
                "grib_run_initialization_utc":
                    run.isoformat().replace("+00:00", "Z"),
                "temperature_2m_c":
                    20.0 if station_id == "KATL" else 25.0,
            }]
        }

    monkeypatch.setattr(gfs, "_message", fake_message)
    monkeypatch.setattr(
        gfs, "decode_native_temperature_points", fake_decode
    )
    monkeypatch.setattr(gfs, "_retained_raw_bytes", lambda _: 0)

    result = gfs.rebuild(
        event_dates=["2026-06-28"],
        station_ids=["KATL", "RKPK"],
        station_timezones={
            "KATL": "America/New_York",
            "RKPK": "Asia/Seoul",
        },
        output_root=tmp_path / "raw",
        output_path=tmp_path / "evidence.json",
        as_of_utc="2026-06-30T00:00:00Z",
        run_cycle="18Z",
        run_day_offset=2,
    )

    assert result["target_timezone"] == "REGISTRY_BOUND_PER_STATION"
    assert result["checks"]["mixed_timezone_batch"] is True
    assert result["curves"]["complete_curve_count"] == 2
    assert result["model_row_count"] == 2
    assert {
        row["target_timezone"] for row in result["model_points"]
    } == {"America/New_York", "Asia/Seoul"}
    assert result["message_count"] == len(message_times)


def test_gfs_resume_reuses_messages_and_decodes_only_missing_station_points(
    tmp_path,
    monkeypatch,
) -> None:
    run = datetime(2026, 6, 26, 18, tzinfo=timezone.utc)
    start = datetime(2026, 6, 28, 4, tzinfo=timezone.utc)
    valid_times = [
        start + timedelta(hours=offset)
        for offset in gfs.aligned_target_sample_offsets(
            target_day_start_utc=start,
            run_initialization=run,
        )
    ]
    records = []
    existing_points = []
    raw_by_valid = {}
    for index, valid in enumerate(valid_times):
        raw_path = tmp_path / f"{index}.grib2"
        raw_path.write_bytes(b"GRIB")
        valid_text = valid.isoformat().replace("+00:00", "Z")
        raw_by_valid[str(raw_path)] = valid_text
        token = str(index).zfill(64)
        records.append({
            "provider_id": gfs.GFS_PROVIDER,
            "event_date": "2026-06-28",
            "run_initialization_utc": "2026-06-26T18:00:00Z",
            "valid_time_utc": valid_text,
            "source_url": "https://example.test/gfs",
            "raw_message_sha256": token,
            "receipt_sha256": token[::-1],
            "raw_object_ref": str(raw_path),
            "published_at_utc": "2026-06-27T00:00:00Z",
        })
        existing_points.append({
            "station_id": "KATL",
            "event_date": "2026-06-28",
            "target_timezone": "America/New_York",
            "provider_id": gfs.GFS_PROVIDER,
            "model_run_initialization_utc": "2026-06-26T18:00:00Z",
            "valid_time_utc": valid_text,
            "temperature_2m_c": 20.0,
            "raw_message_sha256": token,
            "receipt_sha256": token[::-1],
            "published_at_utc": "2026-06-27T00:00:00Z",
            "received_at_utc": "",
            "raw_object_ref": str(raw_path),
        })

    monkeypatch.setattr(
        gfs,
        "_resume_rows",
        lambda *args, **kwargs: (
            list(existing_points),
            list(records),
            {
                ("2026-06-28", item["valid_time_utc"])
                for item in records
            },
        ),
    )
    monkeypatch.setattr(
        gfs,
        "_message",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("resume_must_not_refetch_existing_message")
        ),
    )
    decoded_stations = []

    def fake_decode(raw_path, *, provider_id, station_id):
        del provider_id
        decoded_stations.append(station_id)
        return {
            "points": [{
                "grib_valid_time_utc": raw_by_valid[str(raw_path)],
                "grib_run_initialization_utc": "2026-06-26T18:00:00Z",
                "temperature_2m_c": 25.0,
            }]
        }

    monkeypatch.setattr(
        gfs, "decode_native_temperature_points", fake_decode
    )
    monkeypatch.setattr(gfs, "_retained_raw_bytes", lambda _: 0)

    result = gfs.rebuild(
        event_dates=["2026-06-28"],
        station_ids=["KATL", "RKPK"],
        target_timezone="America/New_York",
        output_root=tmp_path,
        output_path=tmp_path / "evidence.json",
        resume_path=tmp_path / "not-read-because-monkeypatched.json",
        as_of_utc="2026-06-30T00:00:00Z",
        run_cycle="18Z",
        run_day_offset=2,
    )

    assert decoded_stations == ["RKPK"] * len(valid_times)
    assert result["message_count"] == len(valid_times)
    assert result["model_point_count"] == len(valid_times) * 2
    assert result["curves"]["complete_curve_count"] == 2


def test_ecmwf_18z_archive_urls_preserve_real_cycle() -> None:
    run = ecmwf._run_initialization(
        date(2026, 7, 14),
        run_cycle="18Z",
        run_day_offset=2,
    )
    urls = ecmwf._candidate_urls(run, 34)
    assert urls[0].endswith(
        "/20260712/18z/ifs/0p25/oper/20260712180000-34h-oper-fc.grib2"
    )
    assert urlsplit(urls[0]).netloc == ecmwf.ARCHIVE_HOST
    assert urls[1].endswith(
        "/20260712/18z/ifs/0p25/oper/20260712180000-34h-oper-fc.grib2"
    )
    assert urlsplit(urls[1]).netloc == ecmwf.HOST


def test_ecmwf_mixed_timezone_batch_binds_each_station_local_day(
    tmp_path,
    monkeypatch,
) -> None:
    message_times = {}

    def fake_extract(*, url, output_root, lead_hours):
        del output_root
        valid = datetime(2026, 6, 26, 18, tzinfo=timezone.utc) + timedelta(
            hours=lead_hours
        )
        raw_path = tmp_path / f"{lead_hours}.grib2"
        raw_path.write_bytes(b"GRIB")
        message_times[str(raw_path)] = valid
        token = str(lead_hours).zfill(64)
        return {
            "provider_id": ecmwf.PROVIDER,
            "source_url": url,
            "raw_message_sha256": token,
            "raw_object_ref": str(raw_path),
            "published_at_utc": "2026-06-27T00:00:00Z",
        }, raw_path

    def fake_decode(raw_path, *, provider_id, station_id):
        del provider_id
        valid = message_times[str(raw_path)]
        return {
            "points": [{
                "grib_valid_time_utc":
                    valid.isoformat().replace("+00:00", "Z"),
                "grib_run_initialization_utc": "2026-06-26T18:00:00Z",
                "temperature_2m_c":
                    20.0 if station_id == "KATL" else 25.0,
            }]
        }

    monkeypatch.setattr(ecmwf, "_stream_extract", fake_extract)
    monkeypatch.setattr(
        ecmwf, "decode_native_temperature_points", fake_decode
    )
    monkeypatch.setattr(ecmwf, "_raw_bytes", lambda _: 0)

    result = ecmwf.rebuild(
        event_dates=["2026-06-28"],
        station_ids=["KATL", "RKPK"],
        station_timezones={
            "KATL": "America/New_York",
            "RKPK": "Asia/Seoul",
        },
        output_root=tmp_path / "raw",
        output_path=tmp_path / "evidence.json",
        as_of_utc="2026-06-30T00:00:00Z",
        run_cycle="18Z",
        run_day_offset=2,
    )

    assert result["target_timezone"] == "REGISTRY_BOUND_PER_STATION"
    assert result["checks"]["mixed_timezone_batch"] is True
    assert result["curves"]["complete_curve_count"] == 2
    assert result["model_row_count"] == 2
    assert {
        row["target_timezone"] for row in result["model_points"]
    } == {"America/New_York", "Asia/Seoul"}
    assert result["message_count"] == len(message_times)


def test_ecmwf_mixed_timezone_resume_rejects_scope_drift(
    tmp_path,
) -> None:
    resume = tmp_path / "resume.json"
    resume.write_text(
        json.dumps({
            "schema": ecmwf.SCHEMA,
            "target_timezone": "REGISTRY_BOUND_PER_STATION",
            "station_ids": ["KATL", "RKPK"],
            "station_timezones": {
                "KATL": "America/New_York",
                "RKPK": "Asia/Seoul",
            },
            "message_records": [],
            "model_points": [],
        }),
        encoding="utf-8",
    )

    with pytest.raises(
        ContractError,
        match="resume_station_timezone_mismatch",
    ):
        ecmwf.rebuild(
            event_dates=["2026-06-28"],
            station_ids=["KATL", "RKPK"],
            station_timezones={
                "KATL": "America/New_York",
                "RKPK": "Asia/Tokyo",
            },
            output_root=tmp_path / "raw",
            output_path=tmp_path / "evidence.json",
            resume_path=resume,
            as_of_utc="2026-06-30T00:00:00Z",
            run_cycle="18Z",
            run_day_offset=2,
        )


def test_calibration_truth_dataset_is_a_kst_settlement_authority(tmp_path) -> None:
    identity = {
        "schema": "bayes_basket_station_calibration_dataset_v1",
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "samples": [
            {"event_date": "2026-07-01"},
            {"event_date": "2026-07-02"},
        ],
    }
    path = tmp_path / "rkpk-truth.json"
    path.write_text(
        json.dumps({**identity, "content_sha256": content_sha256(identity)}),
        encoding="utf-8",
    )

    kwargs = {
        "path": path,
        "station_ids": ["RKPK"],
        "event_dates": ["2026-07-01", "2026-07-02"],
    }
    assert gfs._calibration_truth_timezone(**kwargs) == "Asia/Seoul"
    assert ecmwf._calibration_truth_timezone(**kwargs) == "Asia/Seoul"

    with pytest.raises(ContractError, match="truth_dates_missing"):
        gfs._calibration_truth_timezone(
            path,
            station_ids=["RKPK"],
            event_dates=["2026-06-30"],
        )


def test_ecmwf_rebuild_creates_new_output_root_before_resume(
    tmp_path,
    monkeypatch,
) -> None:
    output_root = tmp_path / "new" / "nested" / "raw-root"
    output = tmp_path / "evidence.json"

    def assert_root_exists(resume_path, *, event_dates, output_root, target_timezone):
        del resume_path, event_dates
        assert target_timezone == "America/New_York"
        assert output_root.is_dir()
        return [], [], set()

    monkeypatch.setattr(ecmwf, "_resume_rows", assert_root_exists)
    monkeypatch.setattr(
        ecmwf,
        "_stream_extract",
        lambda **kwargs: (_ for _ in ()).throw(
            ContractError("expected_test_stop")
        ),
    )

    result = ecmwf.rebuild(
        event_dates=["2026-07-14"],
        station_ids=["CYYZ"],
        output_root=output_root,
        output_path=output,
        target_timezone="America/New_York",
        as_of_utc="2026-07-26T00:00:00Z",
        run_cycle="18Z",
        run_day_offset=1,
    )

    assert output_root.is_dir()
    assert result["message_count"] == 0
    assert len(result["blockers"]) == 5


@pytest.mark.parametrize(
    ("module", "provider"),
    [
        (ecmwf, ecmwf.PROVIDER),
        (gfs, gfs.GFS_PROVIDER),
    ],
)
def test_global_message_reuse_is_sha_and_run_scope_bound(
    tmp_path,
    module,
    provider,
) -> None:
    raw_root = tmp_path / "raw-root"
    raw_path = raw_root / provider / "raw" / "message.grib2"
    raw_path.parent.mkdir(parents=True)
    body = b"GRIB-global-message"
    raw_path.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps({
            "schema": module.SCHEMA,
            "provider_id": provider,
            "run_cycle": "18Z",
            "run_day_offset": 2,
            "message_records": [{
                "provider_id": provider,
                "event_date": "2026-06-29",
                "run_initialization_utc": "2026-06-27T18:00:00Z",
                "valid_time_utc": "2026-06-28T15:00:00Z",
                "raw_message_sha256": sha,
                "raw_object_ref": str(raw_path),
                "byte_range": {
                    "start": 100,
                    "end": 100 + len(body) - 1,
                },
            }],
        }),
        encoding="utf-8",
    )

    records = module._reuse_message_records(
        [evidence],
        event_dates={"2026-06-29"},
        output_root=raw_root,
        run_cycle="18Z",
        run_day_offset=2,
    )
    assert len(records) == 1
    assert records[0]["raw_message_sha256"] == sha

    with pytest.raises(ContractError, match="reuse_evidence_scope_invalid"):
        module._reuse_message_records(
            [evidence],
            event_dates={"2026-06-29"},
            output_root=raw_root,
            run_cycle="00Z",
            run_day_offset=2,
        )


def test_icon_single_run_url_has_explicit_18z_initialization() -> None:
    url = icon._url(
        "CYYZ",
        [date(2026, 7, 14)],
        run_cycle="18Z",
        run_day_offset=2,
    )
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "single-runs-api.open-meteo.com"
    assert query["run"] == ["2026-07-12T18:00"]
    assert query["models"] == ["dwd_icon_global"]
    assert query["forecast_days"] == ["4"]


def test_cma_single_run_url_has_explicit_18z_initialization() -> None:
    url = icon._url(
        "MPMG",
        [date(2026, 7, 29)],
        run_cycle="18Z",
        run_day_offset=2,
        target_timezone="America/Panama",
        model_id="CMA",
    )
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    assert parsed.netloc == "single-runs-api.open-meteo.com"
    assert query["models"] == ["cma_grapes_global"]
    assert query["run"] == ["2026-07-27T18:00"]


def test_icon_single_run_rejects_mixed_event_dates() -> None:
    with pytest.raises(
        ContractError,
        match="single_run_one_event_date_required",
    ):
        icon._url(
            "CYYZ",
            [date(2026, 7, 14), date(2026, 7, 15)],
            run_cycle="18Z",
            run_day_offset=2,
        )


def test_icon_single_run_rows_keep_explicit_cycle_provenance(tmp_path) -> None:
    run = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)
    times = [
        (run + timedelta(hours=offset)).isoformat(timespec="minutes").replace(
            "+00:00",
            "",
        )
        for offset in range(96)
    ]
    body = json.dumps(
        {
            "latitude": 43.625,
            "longitude": -79.625,
            "timezone": "GMT",
            "hourly_units": {"time": "iso8601", "temperature_2m": "°C"},
            "hourly": {
                "time": times,
                "temperature_2m": [20.0 + (offset % 12) for offset in range(96)],
            },
        },
        sort_keys=True,
    ).encode()
    raw = tmp_path / "raw.json"
    raw.write_bytes(body)
    rows, curves, receipt = icon._extract_station(
        station="CYYZ",
        event_dates=[date(2026, 7, 14)],
        url="https://single-runs-api.open-meteo.com/v1/forecast?run=2026-07-12T18%3A00",
        body=body,
        http_status="200",
        raw_path=raw,
        reconstructed_at="2026-07-26T05:00:00Z",
        run_cycle="18Z",
        run_day_offset=2,
        target_timezone="America/New_York",
    )
    assert len(rows) == 1
    assert len(curves) == 1
    assert rows[0]["model_run_initialization_utc"] == "2026-07-12T18:00Z"
    assert rows[0]["run_initialization_provenance"] == (
        "aggregator_single_run_parameter_explicit"
    )
    assert receipt["run_cycle"] == "18Z"
    assert receipt["native_publication_time_observed"] is False


def test_icon_single_run_resume_reuses_complete_station_days(tmp_path) -> None:
    raw_root = tmp_path / icon.PROVIDER_ID / "raw"
    raw_root.mkdir(parents=True)
    raw = raw_root / ("a" * 64 + ".json")
    raw.write_text("{}", encoding="utf-8")
    row = {
        "station_id": "CYYZ",
        "event_date": "2026-07-14",
        "raw_object_ref": str(raw),
    }
    curve = {
        "station_id": "CYYZ",
        "event_date": "2026-07-14",
        "raw_object_ref": str(raw),
    }
    payload = {
        "schema": icon.SCHEMA,
        "target_timezone": "America/New_York",
        "model_rows": [row],
        "curves": {"eligible_model_rows": [curve]},
        "receipts": [{
            "station_id": "CYYZ",
            "event_dates": ["2026-07-14"],
        }],
    }
    resume = tmp_path / "resume.json"
    resume.write_text(json.dumps(payload), encoding="utf-8")
    rows, curves, receipts, keys, _sha = icon._load_resume(
        resume,
        output_root=tmp_path,
        event_dates={"2026-07-14", "2026-07-15"},
        stations={"CYYZ"},
        target_timezone="America/New_York",
    )
    assert len(rows) == len(curves) == len(receipts) == 1
    assert keys == {("CYYZ", "2026-07-14")}
