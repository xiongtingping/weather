"""One bad TAF capture costs its own scope, never every station's P0-M.

2026-09-20 03:28Z: three TGFTP captures with no decoded weather (KMIA x2,
ZSPD) made the forward-TAF ingest raise, the raise failed the chain after P0,
and no station got a P0-M until a fix was restarted in at 05:25:32Z -- two
hours.  It was the third check fixed one at a time; the per-capture checks
left in both loops (the fact ingest and P0-M's own read of the inbox) still
raised.  Now each capture's checks are its own: a refused capture is named
in the receipt and the good ones are materialised as before.
"""
from __future__ import annotations

import json
import sqlite3

from bayes_basket_target.contracts import ProbabilityEnvelope, content_sha256
from bayes_basket_target.probability_bridge import materialize_probability_bridge
from bayes_basket_target.ssot import apply_production_ssot_schema
from tests.test_bayes_basket_target_production_worker import (
    _seed_target_registry_source_lane,
    _taf_live_compatibility_lineage,
    _worker,
)


def _root(conn: sqlite3.Connection) -> None:
    _seed_target_registry_source_lane(
        conn, station_id="RKPK", event_date="2026-07-27",
        settlement_timezone="Asia/Seoul", source_lane="awc",
    )
    root = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0-rkpk-refused-alone",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-26T10:00:00Z",
        "fresh_until_utc": "2026-07-28T00:00:00Z",
        "bucket_ordinals": {"34": 0, "35": 1, "36": 2},
        "probabilities": {"34": 0.2, "35": 0.6, "36": 0.2},
        "posterior_version": "p0-taf-refused-alone-test",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "settlement_timezone": "Asia/Seoul",
            "season_bin": "summer",
            "lead_bucket": "d2_fallback",
            "run_cycle": "18Z",
            "settlement_definition": "official_daily_tmax_c",
            "bucket_centers_c_json": json.dumps(
                {"34": 34.0, "35": 35.0, "36": 36.0},
                sort_keys=True, separators=(",", ":"),
            ),
            "bucket_intervals_c_json": json.dumps(
                {"34": [None, 34.5], "35": [34.5, 35.5], "36": [35.5, None]},
                sort_keys=True, separators=(",", ":"),
            ),
            **_taf_live_compatibility_lineage(),
        },
    })
    materialize_probability_bridge(conn, root, created_at_utc=root.decision_at_utc)


def _capture(station: str) -> dict:
    identity = {
        "schema": "bayes_basket_target_forward_taf_capture_v1",
        "source_class": "official_forward_native_receipt",
        "station_id": station,
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "forecast_revision": {
            "schema": "bayes_basket_forecast_revision_v1",
            "revision_id": f"taf-{station.lower()}-refused-alone-1",
            "station_id": station,
            "event_date": "2026-07-27",
            "revision_kind": "TAF",
            "sequence": 1,
            "issued_at_utc": "2026-07-26T11:00:00Z",
            "received_at_utc": "2026-07-26T11:05:00Z",
            "valid_from_utc": "2026-07-26T11:00:00Z",
            "valid_until_utc": "2026-07-27T18:00:00Z",
            "source_object_sha256": "c" * 64,
            "supersedes_revision_id": None,
        },
        "taf_fields": {
            "tx_c": 36.0,
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
        },
        "boundary": {
            "native_local_receipt": True,
            "retrospective_replay_only": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_a_corrupt_capture_does_not_hold_another_stations_p0m(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    _root(conn)
    corrupt = {**_capture("ZSPD"), "content_sha256": "0" * 64}
    inbox = tmp_path / "taf-inbox.json"
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [corrupt, "not a capture", _capture("RKPK")],
    }), encoding="utf-8")
    worker = _worker(tmp_path, conn)
    worker.taf_inbox_path = inbox

    result = worker._materialize_resident_p0m(as_of_utc="2026-07-26T12:00:00Z")

    assert result["refused_capture_count"] == 2
    assert {r["reason"] for r in result["refused_captures"]} == {
        "resident_taf_capture_content_sha_mismatch",
        "resident_taf_capture_invalid",
    }
    ingress = result["taf_fact_ingress"]
    assert ingress["rejected_count"] == 2 and ingress["wrote_count"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope"
        " WHERE stage='P0-M' AND station_id='RKPK'"
    ).fetchone()[0] == 1
    worker.close()


def test_p0m_reads_a_revisions_newest_capture(tmp_path) -> None:
    # 「以最新的为准」: two captures of one revision in the inbox; the older is
    # set aside by name and the newer is the one P0-M and the facts carry.
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    _root(conn)
    older = _capture("RKPK")
    newer_identity = {k: v for k, v in older.items() if k != "content_sha256"}
    newer_identity = json.loads(json.dumps(newer_identity))
    newer_identity["forecast_revision"]["received_at_utc"] = "2026-07-26T11:20:00Z"
    newer = {**newer_identity, "content_sha256": content_sha256(newer_identity)}
    inbox = tmp_path / "taf-inbox.json"
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [newer, older],
    }), encoding="utf-8")
    worker = _worker(tmp_path, conn)
    worker.taf_inbox_path = inbox

    result = worker._materialize_resident_p0m(as_of_utc="2026-07-26T12:00:00Z")

    assert [r["reason"] for r in result["refused_captures"]] == [
        "forward_taf_capture_superseded_in_inbox"
    ]
    assert conn.execute(
        "SELECT received_at_utc FROM calibration_forward_taf_capture WHERE station_id='RKPK'"
    ).fetchall() == [("2026-07-26T11:20:00Z",)]
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope"
        " WHERE stage='P0-M' AND station_id='RKPK'"
    ).fetchone()[0] == 1
    worker.close()
