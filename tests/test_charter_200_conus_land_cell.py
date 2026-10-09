"""Charter 200: a CONUS station is read at the nearest cell its model calls land.

KSFO's nearest HRRR cell is San Francisco Bay in HRRR's own LAND:surface mask
and read the airport's day maximum 3.75 C cold (sd 1.64) on the P1 lane,
where every other HRRR station read -1.1..+2.0.  The rule moves only a
station whose nearest cell is water; every other station must be read at
exactly the coordinates -- and so the cell and the value -- it was before.

The rule's arithmetic is held on synthetic grids (both scanning orders), the
lane's use of it with the transport faked, and the retired rows' treatment on
an in-memory table: no network, no GRIB fixture.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import noaa_conus_archive as A  # noqa: E402
from bayes_basket_target import regional_point_lane as L  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402

NX, NY = 6, 5
STEP = 0.01          # ~1.1 km: several cells inside the 10 km search


class _Lib:
    def codes_handle_new_from_message(self, *_a):
        return 1

    def codes_handle_delete(self, *_a):
        return 0


def _mask_grid(monkeypatch, land_iter: np.ndarray, *, alternate: bool, identity: str):
    """A regular grid near KSFO; ``land_iter`` is the mask in iterator order."""

    lats = np.repeat(37.58 + STEP * np.arange(NY), NX)
    lons = np.tile(-122.42 + STEP * np.arange(NX), NY) % 360.0
    stored = land_iter.astype(float).copy()
    if alternate:
        for row in range(NY):
            if row % 2 == 1:
                stored[row * NX:(row + 1) * NX] = land_iter[row * NX:(row + 1) * NX][::-1]
    longs = {b"Nx": NX, b"alternativeRowScanning": 1 if alternate else 0, b"bitmapPresent": 0}
    arrays = {b"latitudes": lats, b"longitudes": lons, b"values": stored}
    reads: list[bytes] = []

    def _array(lib, h, k):
        reads.append(k)
        return arrays[k]

    monkeypatch.setattr(A, "_eccodes", lambda: _Lib())
    monkeypatch.setattr(A.GridPointReader, "_long", staticmethod(lambda lib, h, k: longs[k]))
    monkeypatch.setattr(A.GridPointReader, "_array", staticmethod(_array))
    monkeypatch.setattr(A.GridPointReader, "_grid_identity", staticmethod(lambda lib, h: identity))
    monkeypatch.setattr(A, "_SAMPLING_CACHE", {})
    return lats, lons, reads


def _cell(row: int, col: int) -> int:
    return row * NX + col


@pytest.mark.parametrize("alternate", [False, True])
def test_a_station_on_land_is_read_where_it_stands_bit_for_bit(monkeypatch, alternate):
    land = np.ones(NX * NY)
    lats, lons, _ = _mask_grid(monkeypatch, land, alternate=alternate, identity="g-land")
    point = (37.6196123, -122.3656111)      # not on a cell centre
    [resolved] = A.nearest_land_points(b"mask", [point])
    assert resolved["moved"] is False
    # The very floats that went in -- not the cell centre, not a rounding.
    assert (resolved["latitude"], resolved["longitude"]) == point


@pytest.mark.parametrize("alternate", [False, True])
def test_a_water_nearest_cell_moves_to_the_nearest_land_cell(monkeypatch, alternate):
    land = np.ones(NX * NY)
    water = _cell(2, 3)
    land[water] = 0.0
    land[_cell(2, 4)] = 0.0                  # the east neighbour is water too
    lats, lons, _ = _mask_grid(monkeypatch, land, alternate=alternate, identity="g-shore")
    # Just north-east of the water cell's centre: its nearest land cell is
    # the one to the north (row 3, col 3), not the equally-indexed south one.
    point = (float(lats[water]) + 0.004, float(lons[water]) - 360.0 + 0.001)
    [resolved] = A.nearest_land_points(b"mask", [point])
    north = _cell(3, 3)
    assert resolved["moved"] is True
    assert resolved["latitude"] == float(lats[north])
    assert resolved["longitude"] == pytest.approx(float(lons[north]) - 360.0)
    assert 0.0 < resolved["distance_km"] < A.MAX_LAND_SEARCH_KM
    assert resolved["rule"] == A.SAMPLING_RULE


def test_no_land_in_reach_is_not_read_at_all(monkeypatch):
    land = np.zeros(NX * NY)
    land[_cell(0, 0)] = 1.0
    monkeypatch.setattr(A, "MAX_LAND_SEARCH_KM", 1.0)
    lats, lons, _ = _mask_grid(monkeypatch, land, alternate=False, identity="g-sea")
    point = (float(lats[_cell(4, 5)]), float(lons[_cell(4, 5)]) - 360.0)
    assert A.nearest_land_points(b"mask", [point]) == [None]


def test_a_grid_is_resolved_once_per_pass(monkeypatch):
    land = np.ones(NX * NY)
    _lats, _lons, reads = _mask_grid(monkeypatch, land, alternate=False, identity="g-once")
    A.nearest_land_points(b"mask", [(37.6, -122.4)])
    first = len(reads)
    A.nearest_land_points(b"mask", [(37.6, -122.4)])
    assert first > 0 and len(reads) == first, "the static mask is not re-read for a cached point"


def test_a_product_without_a_mask_reads_every_point_where_it_stands(monkeypatch):
    def no_network(*_a, **_k):
        raise AssertionError("the NBM has no mask to fetch")

    monkeypatch.setattr(A, "_get", no_network)
    monkeypatch.setattr(A, "_range_get", no_network)
    points = [(47.44467, -122.31442), (37.61961, -122.36561)]
    out = A.sampling_points(
        product="NBM", run_init=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
        forecast_hour=1, points=points,
    )
    assert [(p["latitude"], p["longitude"]) for p in out] == points
    assert not any(p["moved"] for p in out)


def test_an_unreadable_hrrr_mask_refuses_rather_than_reading_the_water(monkeypatch):
    monkeypatch.setattr(A, "_mark_range", lambda url, mark: None)
    with pytest.raises(ContractError, match="noaa_conus_land_mask_unavailable"):
        A.land_mask_message("HRRR", datetime(2026, 9, 20, 12, tzinfo=timezone.utc), 6)


def test_the_mask_read_asks_for_the_land_line_of_the_same_file(monkeypatch):
    asked: list[tuple[str, str]] = []

    def mark_range(url, mark):
        asked.append((url, mark))
        return (10, 20)

    monkeypatch.setattr(A, "_mark_range", mark_range)
    monkeypatch.setattr(A, "_range_get", lambda url, s, e: b"LAND")
    run = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    assert A.land_mask_message("HRRR", run, 7) == b"LAND"
    assert asked == [(A.file_url("HRRR", run, 7), ":LAND:surface:")]


def test_one_idx_parse_finds_either_line(monkeypatch):
    # The temperature read is still the temperature line.
    monkeypatch.setattr(A, "_get", lambda url: (
        b"1:0:d=2026092012:LAND:surface:7 hour fcst:\n"
        b"2:50:d=2026092012:TMP:2 m above ground:7 hour fcst:\n"
        b"3:90:d=2026092012:POT:2 m above ground:7 hour fcst:\n"
    ))
    assert A._field_range("u") == (50, 90)
    assert A._mark_range("u", A.LAND_MARK) == (0, 50)


# ---------------------------------------------------------------------------
# the lane
# ---------------------------------------------------------------------------

def _targets():
    return [
        {"station_id": "KSEA", "latitude": 47.44467, "longitude": -122.31442,
         "event_date": date(2026, 9, 21), "utc_offset_hours": -7},
        {"station_id": "KSFO", "latitude": 37.61961, "longitude": -122.36561,
         "event_date": date(2026, 9, 21), "utc_offset_hours": -7},
        {"station_id": "KXXX", "latitude": 30.0, "longitude": -90.0,
         "event_date": date(2026, 9, 21), "utc_offset_hours": -5},
    ]


def test_the_lane_reads_each_station_where_the_rule_puts_it(monkeypatch):
    monkeypatch.setattr(A, "product_hours", lambda product, run: [20, 21])
    monkeypatch.setattr(A, "local_day_hours", lambda **_k: [20, 21])
    monkeypatch.setattr(A, "_field_range", lambda url: (0, 1))
    monkeypatch.setattr(A, "_range_get", lambda url, s, e: b"grib")
    asked: list[tuple[str, int]] = []

    def sampling_points(*, product, run_init, forecast_hour, points):
        asked.append((product, forecast_hour))
        return [
            {"latitude": points[0][0], "longitude": points[0][1], "moved": False},
            {"latitude": 37.6003, "longitude": -122.3905, "moved": True},
            None,
        ]

    monkeypatch.setattr(A, "sampling_points", sampling_points)
    built: list[list[tuple[float, float]]] = []
    slow: list[tuple[float, float]] = []

    class Reader:
        def __init__(self, points):
            built.append(list(points))

        def kelvin(self, message):
            return [290.0, None]           # KSEA fast, KSFO slow

    def decode_field(message, *, latitude, longitude, run_init, forecast_hour, url):
        slow.append((latitude, longitude))
        return A.ConusField("x", forecast_hour, 18.0, 0.0, url, 4)

    monkeypatch.setattr(A, "GridPointReader", Reader)
    monkeypatch.setattr(A, "decode_field", decode_field)
    run = datetime(2026, 9, 21, 0, tzinfo=timezone.utc)
    out = L.fetch_noaa_run_for_targets(product="HRRR", run_init=run, targets=_targets())
    assert asked == [("HRRR", 20)], "one mask read, from the first hour this run holds"
    # KSEA: exactly its own coordinates, exactly its old citation.
    assert built == [[(47.44467, -122.31442), (37.6003, -122.3905)]]
    assert out[("KSEA", "2026-09-21")]["source_url"] == A.run_directory_url("HRRR", run)
    assert out[("KSEA", "2026-09-21")]["hourly_c"] == {20: pytest.approx(290.0 - 273.15), 21: pytest.approx(290.0 - 273.15)}
    # KSFO: read at the land cell, fast or slow, and the row says so.
    assert slow == [(37.6003, -122.3905), (37.6003, -122.3905)]
    assert out[("KSFO", "2026-09-21")]["source_url"].endswith("#land_cell=37.60030,-122.39050")
    # No land in reach: not read, not guessed.
    assert ("KXXX", "2026-09-21") not in out


def _conn():
    conn = sqlite3.connect(":memory:")
    L.ensure_schema(conn)
    return conn


def _insert(conn, *, station, version, run_hour, value, knowable, system="NCEP_HRRR_POINT"):
    run = f"2026-09-20T{run_hour:02d}:00:00Z"
    conn.execute(
        f"INSERT INTO {L.TABLE}(regional_point_run_id, ensemble_system_id, station_id, target_local_date,"
        " settlement_timezone, run_init_utc, run_cycle_hour, lead_hours_to_local_day_start, forecast_tmax_c,"
        " remaining_peak_complete, hours_wanted, hours_covered, hourly_c_json, transport_provider_id,"
        " extraction_version, archive_query_at_utc, knowable_at_utc, source_url, schema_version, content_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{station}-{version}-{run_hour}", system, station, "2026-09-21", "America/Los_Angeles", run,
         run_hour, 5.0, value, 1, 1, 1, json.dumps({"2026-09-21T22:00:00Z": value}), "NOAA_HRRR_PDS_S3",
         version, knowable, knowable, "s3://x", L.SCHEMA_VERSION, f"{station}{version}{run_hour}".ljust(64, "0")),
    )


def test_water_cell_rows_are_never_read_and_the_moved_pair_waits_for_its_clock():
    conn = _conn()
    v1 = "ncep_hrrr_surface_nearest_cell_v1"
    start = L.CELL_EVIDENCE_FROM_UTC[("NCEP_HRRR_POINT", "KSFO")]
    _insert(conn, station="KSFO", version=v1, run_hour=6, value=17.0, knowable="2026-09-20T08:00:00Z")
    _insert(conn, station="KSFO", version=L.EXTRACTION_VERSIONS["HRRR"], run_hour=7, value=20.0,
            knowable="2026-09-20T09:00:00Z")
    _insert(conn, station="KSFO", version="ncep_nbm_core_nearest_cell_v1", run_hour=5, value=19.5,
            knowable="2026-09-20T08:00:00Z", system="NCEP_NBM_POINT")
    _insert(conn, station="KSEA", version=v1, run_hour=6, value=21.0, knowable="2026-09-20T08:00:00Z")
    before = L.runs_for(conn, station_id="KSFO", event_date="2026-09-21", as_of_utc="2026-09-21T00:00:00Z")
    assert [r["ensemble_system_id"] for r in before] == ["NCEP_NBM_POINT"], (
        "the water row never, the land row not before the clock: KSFO reads its NBM"
    )
    after = L.runs_for(conn, station_id="KSFO", event_date="2026-09-21",
                       as_of_utc=(datetime.fromisoformat(start.replace("Z", "+00:00"))
                                  + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"))
    assert [(r["ensemble_system_id"], r["forecast_tmax_c"]) for r in after] == [
        ("NCEP_HRRR_POINT", 20.0), ("NCEP_NBM_POINT", 19.5),
    ]
    # Every other station: its v1 rows are read as they always were.
    ksea = L.runs_for(conn, station_id="KSEA", event_date="2026-09-21", as_of_utc="2026-09-21T00:00:00Z")
    assert [(r["ensemble_system_id"], r["forecast_tmax_c"]) for r in ksea] == [("NCEP_HRRR_POINT", 21.0)]
    # A retired row does not hold its run, so a pass fetches it again.
    assert L.stored_run_inits(conn, system_id="NCEP_HRRR_POINT", station_id="KSFO", event_date="2026-09-21") == {
        "2026-09-20T07:00:00Z"
    }
    assert L.stored_run_inits(conn, system_id="NCEP_HRRR_POINT", station_id="KSEA", event_date="2026-09-21") == {
        "2026-09-20T06:00:00Z"
    }


def test_the_land_cell_row_supersedes_the_water_row_on_its_key(monkeypatch, tmp_path):
    conn = _conn()
    audit = tmp_path / "superseded.jsonl"
    monkeypatch.setattr(L, "SUPERSEDED_AUDIT", audit)
    v1 = "ncep_hrrr_surface_nearest_cell_v1"
    _insert(conn, station="KSFO", version=v1, run_hour=6, value=17.0, knowable="2026-09-20T08:00:00Z")
    _insert(conn, station="KSEA", version=v1, run_hour=6, value=21.0, knowable="2026-09-20T08:00:00Z")
    run = datetime(2026, 9, 20, 6, tzinfo=timezone.utc)
    for station in ("KSFO", "KSEA"):
        L.write_run(
            conn, product="HRRR", station_id=station, event_date=date(2026, 9, 21),
            settlement_timezone="America/Los_Angeles", run_init=run, hourly_c={40: 20.0},
            hours_wanted=1, archive_query_at=run + timedelta(days=2), source_url="s3://x", backfill=True,
        )
    rows = dict(conn.execute(
        f"SELECT station_id, extraction_version FROM {L.TABLE} WHERE run_init_utc='2026-09-20T06:00:00Z'"
    ).fetchall())
    assert rows == {"KSFO": L.EXTRACTION_VERSIONS["HRRR"], "KSEA": v1}, (
        "KSFO's water row is replaced; KSEA's v1 row is a land cell and stays"
    )
    lines = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [(x["reason"], x["row"]["station_id"]) for x in lines] == [("water_cell_replaced_by_land_cell", "KSFO")]


def test_only_the_audited_pair_is_retired():
    assert set(L.RETIRED_CELL_VERSIONS) == {("NCEP_HRRR_POINT", "KSFO")}
    assert L.retired_cell_row("NCEP_HRRR_POINT", "ksfo", "ncep_hrrr_surface_nearest_cell_v1")
    assert not L.retired_cell_row("NCEP_HRRR_POINT", "KSFO", L.EXTRACTION_VERSIONS["HRRR"])
    assert not L.retired_cell_row("NCEP_NBM_POINT", "KSFO", "ncep_nbm_core_nearest_cell_v1")
    assert not L.retired_cell_row("NCEP_HRRR_POINT", "KSEA", "ncep_hrrr_surface_nearest_cell_v1")


def test_the_debias_fit_never_trains_on_a_water_cell(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import fit_regional_point_debias as F  # noqa: PLC0415

    db = tmp_path / "ssot.db"
    conn = sqlite3.connect(db)
    L.ensure_schema(conn)
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth(station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        " source_lane TEXT, qc_status TEXT, authority_scope TEXT, revision_no INTEGER)"
    )
    _insert(conn, station="KSFO", version="ncep_hrrr_surface_nearest_cell_v1", run_hour=6, value=17.0,
            knowable="2026-09-20T08:00:00Z")
    _insert(conn, station="KSFO", version=L.EXTRACTION_VERSIONS["HRRR"], run_hour=7, value=20.0,
            knowable="2026-09-20T09:00:00Z")
    _insert(conn, station="KSEA", version="ncep_hrrr_surface_nearest_cell_v1", run_hour=6, value=21.0,
            knowable="2026-09-20T08:00:00Z")
    conn.commit(); conn.close()
    runs, _truth = F.collect(db)
    assert sorted((r["station_id"], r["forecast_tmax_c"]) for r in runs) == [("KSEA", 21.0), ("KSFO", 20.0)]


def test_p1_v20_declares_its_start_and_the_moved_pair_restarts_six_days_later():
    import inspect  # noqa: PLC0415

    from bayes_basket_target import serving_arithmetic as sa  # noqa: PLC0415
    from bayes_basket_target.materialization import materialize_p1  # noqa: PLC0415

    p1 = inspect.signature(materialize_p1).parameters["posterior_version"].default
    # Charters 204 and 210 moved P1 on to v21 and v22 (root bumps); v20 keeps
    # this charter's moment, and the moved pair's clock is six days past it.
    assert p1 == "p1_joint_likelihood_v39"
    start = sa.version_effective_from("P1", "p1_joint_likelihood_v20")
    assert start and start > sa.version_effective_from("P1", "p1_joint_likelihood_v19")
    assert sa.version_effective_from("P1", p1) > start
    clock = L.CELL_EVIDENCE_FROM_UTC[("NCEP_HRRR_POINT", "KSFO")]
    assert datetime.fromisoformat(clock.replace("Z", "+00:00")) == (
        datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(days=6)
    )
