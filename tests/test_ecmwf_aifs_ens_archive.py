"""AIFS ENS official collector: instants, members, daily high, ingest.

Transport and decode are replaced by fakes that encode (member, step) into
the message, so every value the capture reports can be traced back to the
object it came from.  The live check against the research backfill (same
run, same station-days, 51 members agreeing to 1.5e-5 C -- the float32 the
research cache stored) was run on 2026-09-23 and is not repeated here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from bayes_basket_target import ecmwf_aifs_ens_archive as aifs
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema

LENGTH = 64


def _t(day: str, hour: int) -> datetime:
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, tzinfo=timezone.utc)


def _registry_rows(station: str, event_date: str) -> list[dict]:
    buckets = [(None, 19)] + [(v, v) for v in range(20, 25)] + [(25, None)]
    return [
        {
            "station_id": station,
            "event_date": event_date,
            "settlement_unit": "C",
            "outcome": f"b{i}",
            "registry_json": json.dumps({"lower": lo, "upper": hi}),
        }
        for i, (lo, hi) in enumerate(buckets)
    ]


def _index(step: int, kind: str) -> bytes:
    rows = [{"param": "10u", "levtype": "sfc", "type": kind, "step": str(step),
             "_offset": 0, "_length": LENGTH}]
    numbers = [0] if kind == "cf" else list(range(1, 51))
    for n in numbers:
        row = {"param": "2t", "levtype": "sfc", "type": kind, "step": str(step),
               "_offset": 1000 * (n + 1), "_length": LENGTH}
        if kind == "pf":
            row["number"] = str(n)
        rows.append(row)
    return "\n".join(json.dumps(r) for r in rows).encode()


def _value(number: int, step: int) -> float:
    return 10.0 + 0.1 * number + 0.01 * step


@pytest.fixture
def fake_transport(monkeypatch):
    calls: list[str] = []

    def get_bytes(url, *, timeout_seconds, max_bytes, headers=None):
        calls.append(url + "#" + str((headers or {}).get("Range", "")))
        stamp_step = url.rsplit("/", 1)[1].split("-")[1]
        step = int(stamp_step.rstrip("h"))
        kind = "cf" if "-enfo-cf" in url else "pf"
        if url.endswith(".index"):
            return _index(step, kind), {}
        start = int(headers["Range"].split("=")[1].split("-")[0])
        number = start // 1000 - 1
        body = json.dumps({"n": number, "s": step}).encode()
        return (b"GRIB" + body).ljust(LENGTH, b" "), {"last-modified": "x"}

    def decode(message, *, points):
        payload = json.loads(message[4:].decode().strip())
        return {p[0]: _value(payload["n"], payload["s"]) for p in points}

    monkeypatch.setattr(aifs.ifs, "_get_bytes", get_bytes)
    monkeypatch.setattr(aifs, "decode_nearest_temperatures_c", decode)
    return calls


def _scope(station: str, zone: str, event_date: str) -> dict:
    return {
        "station_id": station, "settlement_timezone": zone,
        "latitude": 40.0, "longitude": -73.0,
        "registry_rows": _registry_rows(station, event_date),
    }


def test_day_instants_pre_day_needs_all_four_same_day_takes_what_is_left() -> None:
    # KLGA 2026-09-19 is 04Z..04Z next day: the D-1 00Z run sees 06..24Z+.
    assert aifs.day_instants(
        event_date="2026-09-19", settlement_timezone="America/New_York",
        run_init_utc=_t("2026-09-18", 0),
    ) == [30, 36, 42, 48]
    # A same-day run (issued after local midnight) keeps what remains.
    assert aifs.day_instants(
        event_date="2026-09-19", settlement_timezone="America/New_York",
        run_init_utc=_t("2026-09-19", 18),
    ) == [0, 6]
    # A run whose steps end before the day ends is refused before the day.
    assert aifs.day_instants(
        event_date="2026-09-23", settlement_timezone="America/New_York",
        run_init_utc=_t("2026-09-19", 0),
    ) is None


def test_extraction_version_names_field_cycle_and_lead() -> None:
    assert aifs.extraction_version_for_lead(1, 6) == (
        "ecmwf_open_data_aifs_ens_0p25_2t6_06z_d1_v1"
    )
    assert aifs.extraction_version_for_lead(2) == (
        "ecmwf_open_data_aifs_ens_0p25_2t6_18z_d2_v1"
    )
    with pytest.raises(ContractError):
        aifs.extraction_version_for_lead(3, 18)


def test_parse_index_refuses_a_missing_member() -> None:
    raw = b"\n".join(
        line for line in _index(24, "pf").splitlines() if b'"number": "17"' not in line
    )
    with pytest.raises(ContractError, match="member_index_incomplete"):
        aifs._parse_index(raw, forecast_step=24, kind="pf")
    control = aifs._parse_index(_index(24, "cf"), forecast_step=24, kind="cf")
    assert [row["number"] for row in control] == [0]


def test_capture_reads_each_field_once_and_takes_the_in_day_maximum(fake_transport) -> None:
    archive = aifs.capture_aifs_station_days(
        event_date="2026-09-19",
        station_scopes=[
            _scope("KLGA", "America/New_York", "2026-09-19"),
            _scope("EGLC", "Europe/London", "2026-09-19"),
        ],
        archive_query_at_utc="2026-09-18T06:20:00Z",
        lead_days=1,
        run_cycle_hour=0,
    )
    grib_calls = [u for u in fake_transport if ".grib2#" in u]
    union = archive["union_forecast_steps"]
    assert len(grib_calls) == len(set(grib_calls)) == 51 * len(union)
    by_station = {c["station_id"]: c for c in archive["captures"]}
    klga = by_station["KLGA"]
    assert klga["forecast_steps"] == [30, 36, 42, 48]
    members = {m["member_id"]: m for m in klga["members"]}
    assert len(members) == 51
    assert members["control"]["member_role"] == "CONTROL"
    assert members["control"]["raw_tmax_c"] == pytest.approx(_value(0, 48))
    assert members["perturbed_50"]["raw_tmax_c"] == pytest.approx(_value(50, 48))
    assert klga["field_receipt_count"] == 51 * 4
    assert "family_temperature_curve" not in klga
    assert sum(klga["raw_bucket_probabilities"].values()) == pytest.approx(1.0)
    body = {k: v for k, v in archive.items() if k != "content_sha256"}
    assert archive["content_sha256"] == content_sha256(body)


def test_system_identity_is_the_registered_ssot_row() -> None:
    # The row registered 2026-07-31 under the ECMWF family; charter 217 moved
    # it to AIFS's own family and changed nothing else.
    pre_split = {**aifs.SYSTEM_IDENTITY, "independence_family_id": "ECMWF"}
    assert content_sha256(pre_split) == aifs.PRE_SPLIT_REGISTRY_CONTENT_SHA256 == (
        "d38cd419f5b9ffab939027fd32c33c95d6e471e130afa27e5c64a00410375c3c"
    )
    assert aifs.SYSTEM_IDENTITY["independence_family_id"] == "ECMWF_AIFS"


def test_ingest_writes_51_rows_replays_and_refuses_a_changed_value(fake_transport) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    conn.execute(
        "INSERT INTO calibration_station_registry VALUES(?,?,?,?,?,?,?,?,?)",
        ("KLGA", "KLGA", "New York", "America/New_York", "AWC",
         "official_daily_tmax_c", "v1", "test", "0" * 64),
    )
    archive = aifs.capture_aifs_station_days(
        event_date="2026-09-19",
        station_scopes=[_scope("KLGA", "America/New_York", "2026-09-19")],
        archive_query_at_utc="2026-09-18T06:20:00Z",
        lead_days=1,
        run_cycle_hour=0,
    )
    first = aifs.ingest_aifs_station_days(conn, archive)
    assert first["state"] == "COMMITTED"
    assert first["member_run_wrote_count"] == 51
    rows = conn.execute(
        "SELECT DISTINCT extraction_version, transport_provider_id, run_cycle, "
        "archive_lead_bucket FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id='ECMWF_AIFS_ENS_GLOBAL'"
    ).fetchall()
    assert rows == [(
        "ecmwf_open_data_aifs_ens_0p25_2t6_00z_d1_v1", "ECMWF_OPEN_DATA_S3",
        "00Z", archive["captures"][0]["archive_lead_bucket"],
    )]
    assert aifs.ingest_aifs_station_days(conn, archive)["state"] == "REPLAY"
    capture = dict(archive["captures"][0])
    capture["members"] = [dict(m) for m in capture["members"]]
    capture["members"][0]["raw_tmax_c"] += 0.5
    capture["content_sha256"] = content_sha256(
        {k: v for k, v in capture.items() if k != "content_sha256"}
    )
    with pytest.raises(ContractError, match="frozen_fact_conflict"):
        aifs.ingest_aifs_station_day(conn, capture)
