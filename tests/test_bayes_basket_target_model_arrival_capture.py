from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.model_run_contract import build_model_run_contract  # noqa: E402


SCRIPT = ROOT / "scripts" / "capture_bayes_basket_target_model_arrival.py"


def _module():
    spec = importlib.util.spec_from_file_location("target_arrival_capture", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    status = 200
    headers = {
        "Last-Modified": "Thu, 23 Jul 2020 05:30:00 GMT",
        "ETag": "native-etag",
        "Content-Type": "application/octet-stream",
    }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        if getattr(self, "_read_once", False):
            return b""
        self._read_once = True
        return b"native-grib"


def _args(tmp_path: Path) -> dict[str, object]:
    return {
        "provider_id": "icon_global_official",
        "urls": ["https://opendata.dwd.de/weather/nwp/icon/grib/00/model.grib2.bz2"],
        "publication_url": None,
        "station_id": "KJFK",
        "target_local_date": "2020-07-23",
        "run_initialization_utc": "2020-07-23T00:00:00Z",
        "valid_time_utc": "2020-07-23T16:00:00Z",
        "decision_at_utc": "2099-01-01T00:00:00Z",
        "target_timezone": "America/New_York",
        "output_root": tmp_path,
        "timeout_sec": 1.0,
    }


def test_capture_binds_provider_last_modified_and_local_receive(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    monkeypatch.setattr(module, "open_official_https", lambda _request, timeout_sec: _Response())

    result = module.capture(**_args(tmp_path))

    assert result["status"] == "CAPTURED", result
    arrival = result["arrival"]
    assert arrival["published_at_utc"] == "2020-07-23T05:30:00Z"
    assert arrival["received_at_utc"] <= arrival["decision_at_utc"]
    assert arrival["historical_arrival_time_observed"] is True
    assert Path(result["transport_receipt_path"]).is_file()
    receipt = json.loads(Path(result["transport_receipt_path"]).read_text())
    assert receipt["provider_published_at_utc"] == "2020-07-23T05:30:00Z"
    assert receipt["response_completed_at"] == receipt["raw_receipts"][0]["response_completed_at"]
    assert result["boundary"]["places_orders"] is False
    assert result["boundary"]["calls_order_gateway"] is False


def test_icon_preflight_blocks_unpublished_object_without_opening_get_body(
    monkeypatch, tmp_path: Path
) -> None:
    module = _module()
    methods: list[str] = []

    class NotFoundResponse(_Response):
        status = 404

    def open_response(request, timeout_sec):
        methods.append(request.get_method())
        return NotFoundResponse()

    monkeypatch.setattr(module, "open_official_https", open_response)

    result = module.capture(**_args(tmp_path))

    assert result["status"] == "BLOCKED"
    assert result["receipt"]["reason_codes"] == [
        "model_arrival_icon_preflight_http_404"
    ]
    assert methods == ["HEAD"]
    assert not list(tmp_path.rglob("*.bz2"))


def test_forward_capture_binds_decision_to_observed_response_completion(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    monkeypatch.setattr(module, "open_official_https", lambda _request, timeout_sec: _Response())
    args = _args(tmp_path)
    args["decision_at_utc"] = None

    result = module.capture(**args)

    assert result["status"] == "CAPTURED", result
    arrival = result["arrival"]
    assert arrival["received_at_utc"] == arrival["decision_at_utc"]
    assert arrival["published_at_utc"] <= arrival["received_at_utc"]


def test_capture_fails_closed_when_run_manifest_does_not_match(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    args = _args(tmp_path)
    args["run_contract"] = build_model_run_contract(
        target_local_date="2020-07-23",
        run_initialization_utc="2020-07-23T00:00:00Z",
        station_ids=["KJFK"],
        target_timezone="America/New_York",
    )
    args["run_initialization_utc"] = "2020-07-23T06:00:00Z"

    result = module.capture(**args)

    assert result["status"] == "BLOCKED"
    assert "model_run_contract_run_mismatch" in result["receipt"]["reason_codes"]
    assert result["boundary"]["places_orders"] is False


def test_capture_preserves_bounded_pre_event_raw_without_formal_authority(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    class PreEventResponse(_Response):
        headers = {
            **_Response.headers,
            "Last-Modified": "Thu, 23 Jul 2026 05:30:00 GMT",
        }

    monkeypatch.setattr(module, "open_official_https", lambda _request, timeout_sec: PreEventResponse())
    monkeypatch.setattr(module, "_now", lambda: "2026-07-23T18:00:00Z")
    args = _args(tmp_path)
    args.update({
        "target_local_date": "2026-07-24",
        "run_initialization_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": "2026-07-24T16:00:00Z",
        "decision_at_utc": "2026-07-23T18:00:00Z",
    })

    result = module.capture(**args)

    assert result["status"] == "CAPTURED"
    assert result["arrival"]["capture_phase"] == "prospective_pre_event"
    assert result["arrival"]["p0_consumption_eligible_at_capture"] is True
    assert result["arrival"]["p1_consumption_eligible_at_capture"] is False
    assert result["boundary"]["raw_capture_only"] is True
    assert result["boundary"]["p0_consumption_authorized"] is True
    assert result["boundary"]["p1_consumption_authorized"] is False
    assert Path(result["transport_receipt_path"]).is_file()


def test_capture_blocks_when_native_publication_metadata_is_absent(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class NoPublicationResponse(_Response):
        headers = {"Content-Type": "application/octet-stream"}

    monkeypatch.setattr(module, "open_official_https", lambda _request, timeout_sec: NoPublicationResponse())

    result = module.capture(**_args(tmp_path))

    assert result["status"] == "BLOCKED"
    assert "provider_publication_timestamp_missing" in result["receipt"]["reason_codes"]
    assert result["receipt"]["orders_submitted"] == 0
    assert not list(tmp_path.rglob(".capture.*"))


def test_capture_removes_new_raw_and_receipt_when_decision_clock_is_too_old(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    monkeypatch.setattr(module, "open_official_https", lambda _request, timeout_sec: _Response())
    args = _args(tmp_path)
    args["decision_at_utc"] = "2020-07-23T05:30:01Z"

    result = module.capture(**args)

    assert result["status"] == "BLOCKED"
    assert result["receipt"]["reason_codes"] == ["model_received_after_decision"]
    assert not list(tmp_path.rglob("*.grib2"))
    assert not list(tmp_path.rglob("*.bz2"))
    assert not list(tmp_path.rglob("*.json"))


def test_capture_can_bind_filtered_payload_to_native_metadata_head(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class HeadResponse(_Response):
        headers = {"Last-Modified": "Thu, 23 Jul 2020 05:30:00 GMT"}

    def open_response(request, timeout_sec):
        return HeadResponse() if request.get_method() == "HEAD" else type("PayloadResponse", (_Response,), {"headers": {}})()

    monkeypatch.setattr(module, "open_official_https", open_response)
    args = _args(tmp_path)
    args["publication_url"] = "https://opendata.dwd.de/weather/nwp/icon/grib/00/index.html"

    result = module.capture(**args)

    assert result["status"] == "CAPTURED", result
    assert result["arrival"]["publication_time_source"] == "provider_http_last_modified"


def test_gfs_filtered_payload_can_bind_to_official_noaa_s3_last_modified(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class HeadResponse(_Response):
        headers = {"Last-Modified": "Thu, 23 Jul 2026 05:30:00 GMT"}

    def open_response(request, timeout_sec):
        if request.get_method() == "HEAD":
            assert request.full_url.startswith("https://noaa-gfs-bdp-pds.s3.amazonaws.com/")
            return HeadResponse()
        assert request.full_url.startswith("https://nomads.ncep.noaa.gov/")
        return type("PayloadResponse", (_Response,), {"headers": {}})()

    monkeypatch.setattr(module, "open_official_https", open_response)
    args = _args(tmp_path)
    args.update({
        "provider_id": "gfs_0p25_official",
        "urls": ["https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl?file=gfs.t00z.pgrb2.0p25.f028"],
        "publication_url": "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260723/00/atmos/gfs.t00z.pgrb2.0p25.f028",
        "run_initialization_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": "2026-07-24T04:00:00Z",
        "target_local_date": "2026-07-24",
        "decision_at_utc": "2099-01-01T00:00:00Z",
    })

    result = module.capture(**args)

    assert result["status"] == "CAPTURED", result
    assert result["arrival"]["published_at_utc"] == "2026-07-23T05:30:00Z"
    assert result["arrival"]["publication_time_source"] == "provider_http_last_modified"


def test_capture_records_and_requires_exact_native_byte_range(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class HeadResponse(_Response):
        headers = {"Last-Modified": "Thu, 23 Jul 2026 07:34:00 GMT"}

    class RangeResponse(_Response):
        status = 206
        headers = {
            "Content-Length": "11",
            "Content-Range": "bytes 100-110/144175907",
        }

    def open_response(request, timeout_sec):
        if request.get_method() == "HEAD":
            return HeadResponse()
        assert request.get_header("Range") == "bytes=100-110"
        return RangeResponse()

    monkeypatch.setattr(module, "open_official_https", open_response)
    args = _args(tmp_path)
    args.update({
        "provider_id": "ecmwf_ifs025_official",
        "urls": ["https://data.ecmwf.int/forecasts/20260723/00z/ifs/0p25/oper/20260723000000-30h-oper-fc.grib2"],
        "publication_url": "https://data.ecmwf.int/forecasts/20260723/00z/ifs/0p25/oper/20260723000000-30h-oper-fc.grib2",
        "target_local_date": "2026-07-24",
        "run_initialization_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": "2026-07-24T06:00:00Z",
        "decision_at_utc": "2099-01-01T00:00:00Z",
        "range_start": 100,
        "range_end": 110,
    })

    result = module.capture(**args)

    assert result["status"] == "CAPTURED", result
    assert result["boundary"]["request_byte_range"] == {"start": 100, "end": 110}
    receipt = json.loads(Path(result["transport_receipt_path"]).read_text())
    assert receipt["raw_receipts"][0]["request_byte_range"] == {"start": 100, "end": 110}


def test_capture_blocks_when_provider_ignores_requested_byte_range(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class FullResponse(_Response):
        headers = {"Last-Modified": "Thu, 23 Jul 2026 07:34:00 GMT"}

    monkeypatch.setattr(module, "open_official_https", lambda _request, timeout_sec: FullResponse())
    args = _args(tmp_path)
    args.update({
        "provider_id": "ecmwf_ifs025_official",
        "urls": ["https://data.ecmwf.int/forecasts/20260723/00z/ifs/0p25/oper/20260723000000-30h-oper-fc.grib2"],
        "publication_url": "https://data.ecmwf.int/forecasts/20260723/00z/ifs/0p25/oper/20260723000000-30h-oper-fc.grib2",
        "range_start": 100,
        "range_end": 110,
    })

    result = module.capture(**args)

    assert result["status"] == "BLOCKED"
    assert result["receipt"]["reason_codes"] == ["model_arrival_byte_range_not_honored"]
    assert not list(tmp_path.rglob("*.grib2"))
