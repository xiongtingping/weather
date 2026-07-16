import gzip
import hashlib
import json

from scripts.export_amsc_source_skill_forward import export_amsc_source_skill_forward


def _write_raw(root, *, raw_text, report_type, valid_time, first_seen):
    digest = hashlib.sha256(raw_text.encode()).hexdigest()
    path = root / "objects" / digest[:2] / f"{digest}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(
            {
                "schema": "amsc_raw_report_object_v1",
                "source_id": "zuuu_metar_raw_amsc" if report_type == "METAR" else "zuuu_taf_forecast_amsc",
                "station_id": "ZUUU",
                "report_type": report_type,
                "raw_text": raw_text,
                "raw_sha256": digest,
                "verified_valid_time_utc": valid_time,
                "first_seen_at": first_seen,
                "arrival_time_quality": "observed_first_seen",
            },
            handle,
        )


def test_export_preserves_metar_first_seen_and_taf_immutable_lineage(tmp_path):
    _write_raw(
        tmp_path,
        raw_text="METAR ZUUU 100100Z 18003MPS CAVOK 30/20 Q1001 NOSIG=",
        report_type="METAR",
        valid_time="2026-07-10T01:00:00Z",
        first_seen="2026-07-10T01:01:00Z",
    )
    _write_raw(
        tmp_path,
        raw_text="TAF ZUUU 091800Z 1000/1106 20003MPS CAVOK TX36/1007Z TN27/1022Z=",
        report_type="TAF",
        valid_time="2026-07-10T07:00:00Z",
        first_seen="2026-07-09T18:02:00Z",
    )

    report = export_amsc_source_skill_forward(tmp_path)

    assert report["observation_count"] == 1
    assert report["observations"][0]["as_of_eligible_at"] == "2026-07-10T01:01:00Z"
    assert report["observations"][0]["temperature_c"] == 30.0
    assert report["forecast_snapshot_count"] == 1
    assert report["forecast_snapshots"][0]["forecast_peak_c"] == 36.0
    assert report["forecast_snapshots"][0]["lineage_quality"] == "immutable_external_raw_object"
    assert report["guards"]["reads_archive_db"] is False
