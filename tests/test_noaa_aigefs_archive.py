"""NOAA AIGEFS official collector: index lines, members, daily high, ingest.

Transport and decode are fakes that encode (member, step) into the message;
the live check against the research backfill (2026-09-20 00Z, four stations,
31 members to 1.2e-5 C) was run on 2026-09-24 and is not repeated here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from bayes_basket_target import noaa_aigefs_archive as ag
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema

LENGTH = 64


def _t(day: str, hour: int) -> datetime:
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, tzinfo=timezone.utc)


def _registry_rows(station: str, event_date: str) -> list[dict]:
    buckets = [(None, 19)] + [(v, v) for v in range(20, 25)] + [(25, None)]
    return [
        {"station_id": station, "event_date": event_date, "settlement_unit": "C",
         "outcome": f"b{i}", "registry_json": json.dumps({"lower": lo, "upper": hi})}
        for i, (lo, hi) in enumerate(buckets)
    ]


def _index(run_init: datetime, number: int, step: int) -> bytes:
    stamp = f"d={run_init:%Y%m%d%H}"
    fcst = "anl" if step == 0 else f"{step} hour fcst"
    lines = [
        f"1:0:{stamp}:PRES:surface:{fcst}:ENS=+{number}",
        f"2:1000:{stamp}:TMP:2 m above ground:{fcst}:ENS=+{number}",
        f"3:{1000 + LENGTH}:{stamp}:DPT:2 m above ground:{fcst}:ENS=+{number}",
    ]
    return "\n".join(lines).encode()


def _value(number: int, step: int) -> float:
    return 10.0 + 0.1 * number + 0.01 * step


@pytest.fixture
def fake_transport(monkeypatch):
    calls: list[str] = []
    run_init = _t("2026-09-20", 0)

    def get_bytes(url, *, client, timeout_seconds, max_bytes, headers=None):
        calls.append(url + "#" + str((headers or {}).get("Range", "")))
        key = url.split("/EAGLE_ensemble/", 1)[1]
        number = int(key.split("/mem", 1)[1][:3])
        step = int(key.rsplit(".f", 1)[1][:3])
        if url.endswith(".idx"):
            return _index(run_init, number, step), {}
        body = json.dumps({"n": number, "s": step}).encode()
        message = (b"GRIB" + body).ljust(LENGTH - 4, b" ") + b"7777"
        return message, {"last-modified": "x"}

    def decode(message, *, points):
        payload = json.loads(message[4:-4].decode().strip())
        return {p[0]: _value(payload["n"], payload["s"]) for p in points}

    monkeypatch.setattr(ag.gefs, "_get_bytes", get_bytes)
    monkeypatch.setattr(ag, "decode_nearest_temperatures_c", decode)
    return calls


def _scope(station: str, zone: str, event_date: str) -> dict:
    return {"station_id": station, "settlement_timezone": zone, "latitude": 40.0,
            "longitude": -73.0, "registry_rows": _registry_rows(station, event_date)}


def test_index_line_is_checked_against_run_step_and_member() -> None:
    run = _t("2026-09-20", 0)
    assert ag._parse_index(_index(run, 7, 24), run_init=run, member_number=7, forecast_step=24) == (
        1000, 1000 + LENGTH - 1, f"2:1000:d=2026092000:TMP:2 m above ground:24 hour fcst:ENS=+7",
    )
    with pytest.raises(ContractError, match="index_line_unexpected"):
        ag._parse_index(_index(run, 7, 24), run_init=run, member_number=8, forecast_step=24)
    with pytest.raises(ContractError, match="index_line_unexpected"):
        ag._parse_index(_index(run, 7, 24), run_init=run, member_number=7, forecast_step=30)
    # the analysis step reads "anl", and a last-message range is open ended
    tail = b"\n".join(_index(run, 0, 0).splitlines()[:2])
    assert ag._parse_index(tail, run_init=run, member_number=0, forecast_step=0)[:2] == (1000, None)


def test_object_key_and_extraction_version() -> None:
    assert ag.object_key(_t("2026-09-20", 6), 0, 24) == (
        "EAGLE_ensemble/aigefs.20260920/06/mem000/model/atmos/grib2/aigefs.t06z.sfc.f024.grib2"
    )
    assert ag.extraction_version_for_lead(1, 6) == "noaa_nodd_aigefs_0p25_2t6_06z_d1_v1"
    assert ag.member_id_for(0) == "control" and ag.member_id_for(30) == "perturbed_30"


def test_capture_reads_each_field_once_and_takes_the_in_day_maximum(fake_transport) -> None:
    archive = ag.capture_aigefs_station_days(
        event_date="2026-09-21",
        station_scopes=[_scope("KLGA", "America/New_York", "2026-09-21"),
                        _scope("EGLC", "Europe/London", "2026-09-21")],
        archive_query_at_utc="2026-09-20T07:10:00Z", lead_days=1, run_cycle_hour=0,
    )
    grib_calls = [u for u in fake_transport if ".grib2#" in u]
    union = archive["union_forecast_steps"]
    assert len(grib_calls) == len(set(grib_calls)) == 31 * len(union)
    klga = {c["station_id"]: c for c in archive["captures"]}["KLGA"]
    assert klga["forecast_steps"] == [30, 36, 42, 48]
    members = {m["member_id"]: m for m in klga["members"]}
    assert len(members) == 31 and members["control"]["member_role"] == "CONTROL"
    assert members["control"]["raw_tmax_c"] == pytest.approx(_value(0, 48))
    assert members["perturbed_30"]["raw_tmax_c"] == pytest.approx(_value(30, 48))
    assert klga["field_receipt_count"] == 31 * 4
    assert "family_temperature_curve" not in klga
    assert archive["ensemble_system_id"] == "NOAA_AIGEFS_025"
    assert archive["independence_family_id"] == "NOAA_AIGEFS"
    body = {k: v for k, v in archive.items() if k != "content_sha256"}
    assert archive["content_sha256"] == content_sha256(body)


def test_system_identity_is_the_registered_ssot_row() -> None:
    assert content_sha256(ag.SYSTEM_IDENTITY) == (
        "e00412bc92e11d0c5e0bd4a6ce0b85d60b4f7c010c6ed297798d13e3d364fc29"
    )


def test_ingest_writes_31_rows_replays_and_refuses_a_changed_value(fake_transport) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    conn.execute(
        "INSERT INTO calibration_station_registry VALUES(?,?,?,?,?,?,?,?,?)",
        ("KLGA", "KLGA", "New York", "America/New_York", "AWC", "official_daily_tmax_c", "v1", "test", "0" * 64),
    )
    archive = ag.capture_aigefs_station_days(
        event_date="2026-09-21", station_scopes=[_scope("KLGA", "America/New_York", "2026-09-21")],
        archive_query_at_utc="2026-09-20T07:10:00Z", lead_days=1, run_cycle_hour=0,
    )
    first = ag.ingest_aigefs_station_days(conn, archive)
    assert first["state"] == "COMMITTED" and first["member_run_wrote_count"] == 31
    assert conn.execute(
        "SELECT DISTINCT extraction_version, transport_provider_id, run_cycle FROM "
        "calibration_native_ensemble_historical_member_run WHERE ensemble_system_id='NOAA_AIGEFS_025'"
    ).fetchall() == [("noaa_nodd_aigefs_0p25_2t6_00z_d1_v1", "NOAA_GEFS_PDS_S3", "00Z")]
    assert ag.ingest_aigefs_station_days(conn, archive)["state"] == "REPLAY"
    capture = dict(archive["captures"][0])
    capture["members"] = [dict(m) for m in capture["members"]]
    capture["members"][0]["raw_tmax_c"] += 0.5
    capture["content_sha256"] = content_sha256({k: v for k, v in capture.items() if k != "content_sha256"})
    with pytest.raises(ContractError, match="frozen_fact_conflict"):
        ag.ingest_aigefs_station_day(conn, capture)
