from pathlib import Path

from official_nwp_snapshot_runtime import build_and_persist_official_nwp_snapshot, credential_safe_request_url
from official_nwp_source_catalog_runtime import load_official_nwp_catalog


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json"


def test_credential_safe_url_keeps_nomads_filter_parameters_but_rejects_keys() -> None:
    safe = credential_safe_request_url("https://nomads.ncep.noaa.gov/cgi-bin/filter?file=gfs.grb2&var_TMP=on")
    assert safe.endswith("file=gfs.grb2&var_TMP=on")
    assert credential_safe_request_url("https://nomads.ncep.noaa.gov/file.grb2?token=secret") == ""


def test_persisted_snapshot_requires_direct_host_and_complete_decoder_surface(tmp_path: Path) -> None:
    provider = load_official_nwp_catalog(CATALOG)["providers"]["gfs"]
    raw = tmp_path / "gfs.grib2"
    raw.write_bytes(b"direct-gfs-grib")
    decoded = {
        "ok": True,
        "decoder_id": "eccodes_grib_get_nearest_point_v1",
        "decoder_version": "eccodes 2.41.0",
        "normalized_features": {field: 1.0 for field in provider["required_normalized_fields"]},
        "field_trace": {},
    }

    result = build_and_persist_official_nwp_snapshot(
        catalog_path=CATALOG,
        provider_id="gfs",
        raw_path=raw,
        raw_request_url="https://nomads.ncep.noaa.gov/pub/data/gfs.grib2?var_TMP=on",
        raw_payload_sha256s=["provider-payload-sha"],
        run_at_utc="2026-07-12T06:00:00Z",
        valid_time_utc="2026-07-12T12:00:00Z",
        response_completed_at="2026-07-12T07:45:05Z",
        requested_at="2026-07-12T07:45:00Z",
        response_started_at="2026-07-12T07:45:01Z",
        response_headers_observed=True,
        decoded=decoded,
        current_path=tmp_path / "current.json",
    )

    assert result["ok"] is True, result.get("blockers")
    assert result["snapshot"]["provider_id"] == "gfs"
    assert result["snapshot"]["open_meteo"] is False
    assert result["snapshot"]["response_headers_observed"] is True
    assert result["snapshot"]["response_started_at"] == "2026-07-12T07:45:01Z"
    assert result["snapshot"]["raw_payload_sha256s"] == ["provider-payload-sha"]
    assert Path(result["history_path"]).is_file()
    assert (tmp_path / "current.json").is_file()
