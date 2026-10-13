"""Charter 267 (二)(三)(四): native deterministic runs and the listing-time F.

(二) IFS HRES, GFS, AIGFS and GDPS are read from their publishers' own
objects (never the Open-Meteo relay) into charter 201's run table, captured
by the family's exempt lane, and kept out of the P0 feature.  (三) F is
built from the native runs only, before listing, into one runtime file.
(四) the family's roster takes each station's bound settlement timezone.
"""
from __future__ import annotations

import bz2
import importlib.util
import json
import sqlite3
import struct
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import bayes_basket_target.deterministic_forecast_family as FAM
import bayes_basket_target.listing_forecast_f as F
import bayes_basket_target.native_deterministic_point_archive as N
import bayes_basket_target.regional_point_lane as L

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ("IFS_HRES", "GFS", "AIGFS", "GDPS", "ICON_GLOBAL", "ARPEGE", "GFS_HOURLY", "UKMO_GLOBAL", "GFS_SFLUX")
RUN = datetime(2026, 10, 11, 0, tzinfo=timezone.utc)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# (二) registration
# --------------------------------------------------------------------------

def test_every_native_product_is_registered_in_every_lane_table():
    for product in NATIVE:
        assert product in L.NATIVE_DETERMINISTIC_PRODUCTS
        for table in (L.SYSTEM_IDS, L.TRANSPORTS, L.EXTRACTION_VERSIONS, L.RUN_CYCLE_HOURS,
                      L.PUBLICATION_LAG_HOURS, L.STEP_HOURS, L.LOOKBACK_HOURS):
            assert product in table, (product, table)
        assert L.RUN_CYCLE_HOURS[product] == N.SPECS[product].cycles
        assert L.STEP_HOURS[product] == N.SPECS[product].step_hours
        assert FAM.FAMILY[product]["step_hours"] == N.SPECS[product].step_hours
        assert FAM.FAMILY[product]["relay"] is False


def test_native_runs_feed_f_and_never_the_p0_feature():
    old = {"HRRR", "NBM", "ICON_EU", "MSM", "CMA", "AIFS", "IFS9KM", "UKMO10"}
    assert FAM.FAMILY_SYSTEM_IDS == frozenset(L.SYSTEM_IDS[p] for p in old)
    for product in NATIVE:
        assert L.SYSTEM_IDS[product] not in FAM.FAMILY_SYSTEM_IDS
        # 补记三: ARPEGE is captured, not read by F; GFS_HOURLY is a research arm.
        # 补记六: GFS_SFLUX is F_v3's GFS member, not F's.
        assert (L.SYSTEM_IDS[product] in FAM.F_SYSTEM_IDS) is (product not in ("ARPEGE", "GFS_HOURLY", "UKMO_GLOBAL", "GFS_SFLUX")), product
    assert L.SYSTEM_IDS["AIFS"] in FAM.F_SYSTEM_IDS
    relay = {L.SYSTEM_IDS[p] for p, meta in FAM.FAMILY.items() if meta["relay"]}
    assert not relay & FAM.F_SYSTEM_IDS, "F reads native runs only, never the aggregator relay"


def test_the_native_products_ride_the_family_capture_and_its_doorbell():
    capture = _load("capture_deterministic_forward")
    probe = _load("probe_deterministic_runs")
    # 2026-10-13 (user 「接入采集」): the hourly research arm is captured
    # forward like the rest; it stays out of F (F_SYSTEM_IDS, above).
    for product in NATIVE:
        assert product in capture.PRODUCTS
        assert product in probe.PRODUCTS
    daemon_text = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text()
    start = daemon_text.index("DETERMINISTIC_PROBE_SWEEPS = {")
    block = daemon_text[start: daemon_text.index("}", start)]
    for product in NATIVE:
        assert f'"{product}": "capture_deterministic_forward"' in block


# --------------------------------------------------------------------------
# (二) object addressing and index parsing
# --------------------------------------------------------------------------

def test_step_and_index_urls_follow_each_publisher():
    assert N.step_url("IFS_HRES", RUN, 36).endswith("/20261011/00z/ifs/0p25/oper/20261011000000-36h-oper-fc.grib2")
    assert N.index_url("IFS_HRES", RUN, 36).endswith("-36h-oper-fc.index")
    assert N.step_url("GFS", RUN, 6).endswith("/gfs.20261011/00/atmos/gfs.t00z.pgrb2.0p25.f006")
    assert N.index_url("GFS", RUN, 6).endswith("f006.idx")
    assert N.step_url("AIGFS", RUN, 12).endswith("/aigfs.20261011/00/model/atmos/grib2/aigfs.t00z.sfc.f012.grib2")
    assert N.step_url("GDPS", RUN, 48).endswith(
        "/20261011/WXO-DD/model_gdps/15km/00/048/20261011T00Z_MSC_GDPS_AirTemp_AGL-2m_LatLon0.15_PT048H.grib2"
    )
    assert N.index_url("GDPS", RUN, 48) is None, "GDPS publishes one field a file: read whole"
    assert N.run_directory_url("GDPS", RUN).endswith("/model_gdps/15km/00/")
    with pytest.raises(Exception):
        N.step_url("ICON", RUN, 0)


def test_ecmwf_index_takes_the_surface_message_only():
    text = "\n".join([
        '{"param": "2t", "levtype": "pl", "_offset": 10, "_length": 5}',
        "not json",
        '{"param": "2t", "levtype": "sfc", "_offset": 100, "_length": 50}',
    ])
    assert N.parse_ecmwf_index(text, param="2t") == (100, 50)
    assert N.parse_ecmwf_index(text, param="lsm") is None


def test_noaa_idx_span_runs_to_the_next_record_and_open_at_the_end():
    text = "\n".join([
        "1:0:d=2026101100:HGT:surface:anl:",
        "2:500:d=2026101100:TMP:2 m above ground:6 hour fcst:",
        "3:900:d=2026101100:LAND:surface:anl:",
    ])
    assert N.parse_noaa_idx(text, field="TMP:2 m above ground") == (500, 400)
    assert N.parse_noaa_idx(text, field="LAND:surface") == (900, None)
    assert N.parse_noaa_idx(text, field="RH:2 m above ground") is None


def test_local_day_steps_cover_the_local_day_only():
    # UTC+8: local 2026-10-12 = 10-11 16Z .. 10-12 16Z; 3 h steps from 00Z.
    steps = N.local_day_steps(product="GFS", run_init=RUN, event_date=date(2026, 10, 12), utc_offset_hours=8)
    assert steps == list(range(18, 40, 3))
    six = N.local_day_steps(product="AIGFS", run_init=RUN, event_date=date(2026, 10, 12), utc_offset_hours=8)
    assert six == [18, 24, 30, 36]


# --------------------------------------------------------------------------
# (二) one read per step for every target
# --------------------------------------------------------------------------

def _targets():
    return [
        {"station_id": "RJTT", "event_date": date(2026, 10, 12), "utc_offset_hours": 9,
         "latitude": 35.55, "longitude": 139.78},
        {"station_id": "RJTT", "event_date": date(2026, 10, 13), "utc_offset_hours": 9,
         "latitude": 35.55, "longitude": 139.78},
        {"station_id": "XSEA", "event_date": date(2026, 10, 12), "utc_offset_hours": 0,
         "latitude": 0.0, "longitude": -30.0},
    ]


def test_fetch_reads_each_step_once_and_places_each_station_on_land():
    reads: list[int] = []

    def reader(product, run_init, step):
        reads.append(step)
        return f"m{step}".encode()

    def land(mask, points):
        return [{"latitude": lat, "longitude": lon, "moved": False} if lon > 0 else None for lat, lon in points]

    def decode(message, *, latitude, longitude):
        return 273.15 + int(message.decode()[1:]) / 10.0, 1.0

    out = N.fetch_run_for_targets(
        product="GFS", run_init=RUN, targets=_targets(), reader=reader,
        mask_reader=lambda p, r: b"mask", decode=decode, land=land,
    )
    assert set(out) == {("RJTT", "2026-10-12"), ("RJTT", "2026-10-13")}, "a station no land cell places is left out"
    assert len(reads) == len(set(reads)), "every step is read once for every target it serves"
    day = out[("RJTT", "2026-10-12")]
    assert sorted(day["hourly_c"]) == list(range(15, 39, 3))
    assert day["hourly_c"][15] == pytest.approx(1.5)
    assert day["hours_wanted"] == 8
    assert day["source_url"].endswith("/gfs.20261011/00/atmos/")


def test_a_run_without_its_land_mask_is_not_read():
    called = []
    out = N.fetch_run_for_targets(
        product="IFS_HRES", run_init=RUN, targets=_targets(),
        reader=lambda *a: called.append(a) or b"m0", mask_reader=lambda p, r: None,
        decode=lambda m, **k: (280.0, 1.0), land=lambda m, pts: [None] * len(pts),
    )
    assert out == {} and not called


def test_an_implausible_value_fails_the_run():
    with pytest.raises(Exception, match="out_of_range"):
        N.fetch_run_for_targets(
            product="GFS", run_init=RUN, targets=_targets()[:1], reader=lambda *a: b"m0",
            mask_reader=lambda p, r: b"mask", decode=lambda m, **k: (12.0, 1.0),
            land=lambda m, pts: [{"latitude": a, "longitude": b} for a, b in pts],
        )


def test_the_regular_grid_shortcut_keeps_only_indexes_the_search_confirms(monkeypatch):
    from bayes_basket_target.noaa_conus_archive import GridPointReader

    keys = {b"Ni": 360, b"Nj": 181, b"iScansNegatively": 0, b"jScansPositively": 0, b"jPointsAreConsecutive": 0}
    doubles = {b"latitudeOfFirstGridPointInDegrees": 90.0, b"longitudeOfFirstGridPointInDegrees": 0.0,
               b"iDirectionIncrementInDegrees": 1.0, b"jDirectionIncrementInDegrees": 1.0}
    monkeypatch.setattr(GridPointReader, "_long", staticmethod(lambda lib, h, k: keys.get(k)))
    monkeypatch.setattr(N.RegularLatLonReader, "_string", staticmethod(lambda lib, h, k: "regular_ll"))
    monkeypatch.setattr(N.RegularLatLonReader, "_double", staticmethod(lambda lib, h, k: doubles.get(k)))
    values = [float(i) for i in range(360 * 181)]
    # (10N, 20E) -> row 80, column 20; (-5, 350) -> row 95, column 350.
    good, bad = 80 * 360 + 20, 95 * 360 + 350

    def slow(message, *, latitude, longitude):
        return (values[good], 1.0) if latitude == 10.0 else (values[bad] + 1.0, 1.0)

    reader = N.RegularLatLonReader([(10.0, 20.0), (-5.0, -10.0)], slow=slow)
    assert reader._derive(None, None, b"msg", values) == [good, None], (
        "a station whose arithmetic cell the eccodes search does not confirm goes the slow way"
    )
    monkeypatch.setattr(N.RegularLatLonReader, "_string", staticmethod(lambda lib, h, k: "reduced_gg"))
    assert reader._derive(None, None, b"msg", values) == [None, None]


# --------------------------------------------------------------------------
# (三) F
# --------------------------------------------------------------------------

A, B, C = "SYS_A", "SYS_B", "SYS_C"
NOW = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_regional_point_run(x)")
    conn.execute("INSERT INTO calibration_regional_point_run VALUES(1)")
    return conn


def _patch(monkeypatch, *, today, history, truths, terrain_table=None):
    def maxima(conn, *, station_id, event_date, as_of_utc, settlement_timezone, systems=None):
        table = today if as_of_utc == NOW.strftime("%Y-%m-%dT%H:%M:%SZ") else history.get(event_date, {})
        wanted = set(systems) if systems is not None else set(F.F_SYSTEM_IDS) | {A, B, C}
        return {s: {"tmax_c": v, "run_init_utc": "r", "knowable_at_utc": "k"} for s, v in table.items()
                if s in wanted or s in (A, B, C)}

    monkeypatch.setattr(F, "system_maxima", maxima)
    monkeypatch.setattr(F, "formal_truths", lambda conn, since, until: dict(truths))
    # 补记六: the terrain table is the runtime file; tests hand one in or get none (F_v3 fails closed).
    monkeypatch.setattr(F.terrain, "read", lambda path=None: terrain_table)
    F._HISTORY_CACHE.clear()
    F._HISTORY_CACHE_V3.clear()
    F._HISTORY_GENERATION[0] = None


def test_f_is_the_median_plus_fleet_shift_plus_a_share_of_the_station_correction(monkeypatch):
    history = {date(2026, 10, d): {A: 20.0, B: 21.0, C: 22.0} for d in (1, 2, 3, 4)}
    # Truth 22.5 every day: A is 2.5 cold, B 1.5, C 0.5.
    truths = {("EGLC", f"2026-10-0{d}"): 22.5 for d in (1, 2, 3, 4)}
    _patch(monkeypatch, today={A: 18.0, B: 19.0, C: 21.0}, history=history, truths=truths)
    out = F.compute(_conn(), now=NOW, roster=[("EGLC", 51.5, 0.05, "Europe/London")])
    entry = out["station_days"]["EGLC|2026-10-13"]
    assert entry["state"] == "READY"
    assert entry["raw_median_c"] == pytest.approx(19.0)
    # corrected: 20.5, 20.5, 21.5 -> median 20.5 -> corr 1.5.
    assert entry["station_corr_c"] == pytest.approx(1.5)
    assert out["fleet"]["source"] == "research_fallback", "4 station-days are far short of the fleet floor"
    assert entry["f_c"] == pytest.approx(19.0 + F.RESEARCH_FLEET_C + F.LAMBDA * 1.5)
    assert entry["listing_instant_utc"] == "2026-10-11T05:00:00Z"
    assert out["authority"] == "EXECUTION_LAYER_ONLY"
    # 补记五 fails closed: four settled days are far short of a recalibration.
    assert out["recalibration"]["state"] == "INSUFFICIENT_HISTORY"
    assert entry["recal_c"] is None and entry["f_c"] == entry["f_base_c"]
    # 补记六 fails closed without the terrain table: no F_v3 anywhere.
    assert out["v3"]["state"] == "TERRAIN_TABLE_MISSING" and "v3" not in entry


def test_a_station_day_with_too_few_systems_has_no_f(monkeypatch):
    _patch(monkeypatch, today={A: 18.0, B: 19.0}, history={}, truths={})
    out = F.compute(_conn(), now=NOW, roster=[("EGLC", 51.5, 0.05, "Europe/London")])
    assert {v["state"] for v in out["station_days"].values()} == {"TOO_FEW_SYSTEMS"}


def test_the_fleet_shift_is_measured_once_history_is_deep_enough(monkeypatch):
    days = [date(2026, 9, 15) + timedelta(days=i) for i in range(20)]
    history = {d: {A: 20.0, B: 21.0, C: 22.0} for d in days}
    stations = [f"S{i:03d}" for i in range(12)]
    truths = {(s, d.isoformat()): 22.0 for s in stations for d in days}
    _patch(monkeypatch, today={A: 18.0, B: 19.0, C: 21.0}, history=history, truths=truths)
    out = F.compute(_conn(), now=NOW, roster=[(s, 0.0, 0.0, "UTC") for s in stations])
    assert out["fleet"]["source"] == "native_history"
    assert out["fleet"]["c"] == pytest.approx(1.0) and out["fleet"]["samples"] == 240


def test_station_bias_uses_only_days_settled_before_the_listing_cut(monkeypatch):
    # A day after event-2 is not knowable at listing: it must not move the bias.
    history = {date(2026, 10, d): {A: 20.0, B: 20.0, C: 20.0} for d in (1, 2, 3, 12)}
    truths = {("EGLC", f"2026-10-{d:02d}"): (21.0 if d < 12 else 40.0) for d in (1, 2, 3, 12)}
    _patch(monkeypatch, today={A: 20.0, B: 20.0, C: 20.0}, history=history, truths=truths)
    out = F.compute(_conn(), now=NOW, roster=[("EGLC", 51.5, 0.05, "Europe/London")])
    assert out["station_days"]["EGLC|2026-10-13"]["station_corr_c"] == pytest.approx(1.0)


def test_the_history_cache_drops_when_the_run_table_grows(monkeypatch):
    history = {date(2026, 10, d): {A: 20.0} for d in (1, 2, 3)}
    truths = {("EGLC", f"2026-10-0{d}"): 22.0 for d in (1, 2, 3)}
    _patch(monkeypatch, today={A: 20.0, B: 20.0, C: 20.0}, history=history, truths=truths)
    conn = _conn()
    roster = [("EGLC", 51.5, 0.05, "Europe/London")]
    first = F.compute(conn, now=NOW, roster=roster)["station_days"]["EGLC|2026-10-13"]
    assert first["station_corr_c"] == pytest.approx(0.0), "one system with history: the others carry no bias"
    for d in (1, 2, 3):
        history[date(2026, 10, d)].update({B: 19.0, C: 19.0})
    again = F.compute(conn, now=NOW, roster=roster)["station_days"]["EGLC|2026-10-13"]
    assert again["station_corr_c"] == pytest.approx(0.0), "same generation: cached"
    conn.execute("INSERT INTO calibration_regional_point_run VALUES(2)")
    grown = F.compute(conn, now=NOW, roster=roster)["station_days"]["EGLC|2026-10-13"]
    # corrected: A 22, B 23, C 23 -> median 23 -> corr 3.
    assert grown["station_corr_c"] == pytest.approx(3.0)


# --------------------------------------------------------------------------
# 补记五: the truth recalibration (user 「更准」 10-12)
# --------------------------------------------------------------------------

def test_the_recalibration_fit_recovers_a_planted_conditional_bias():
    rng = np.random.default_rng(3)
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]
    rows = []
    for i in range(900):
        x = (rng.normal(0.5, 0.8), rng.normal(0.0, 0.3), rng.normal(0.0, 2.0))
        y = -0.3 + 0.4 * x[0] + 0.6 * x[1] - 0.1 * x[2] + rng.laplace(0.0, 0.2)
        rows.append((days[i % 30], x, y))
    model = F.fit_recalibration(rows)
    assert model["state"] == "FITTED" and model["rows"] == 900 and model["days"] == 30
    per_unit = np.array(model["beta_per_sd"]) / np.array(model["sd"])
    assert per_unit == pytest.approx([0.4, 0.6, -0.1], abs=0.04)
    assert F.recal_correction(model, (0.0, 0.0, 0.0)) == pytest.approx(-0.3, abs=0.04)
    assert model["in_sample_mae_after_c"] < model["in_sample_mae_before_c"]


def test_the_recalibration_needs_enough_rows_and_days():
    day = date(2026, 9, 1)
    assert F.fit_recalibration([(day, (0.0, 0.0, float(i)), 0.1) for i in range(F.RECAL_MIN_ROWS)]) is None, "one day"
    few = [(day + timedelta(days=i % 20), (0.0, 0.0, float(i)), 0.1) for i in range(F.RECAL_MIN_ROWS - 1)]
    assert F.fit_recalibration(few) is None


def test_the_newest_observation_is_the_one_settled_at_listing():
    # Event 10-13 lists 10-11 05:00Z.  Tokyo's 10-10 ended 10-10 15:00Z: settled.
    # New York's 10-10 ends 10-11 04:00Z, +3 h is after the listing: 10-09 is used.
    for zone, expected in (("Asia/Tokyo", 10.0), ("America/New_York", 9.0)):
        truths = {date(2026, 10, d): (float(d), F._settled_at(date(2026, 10, d), zone)) for d in (8, 9, 10)}
        assert F._obs_before_listing(truths, date(2026, 10, 13)) == expected, zone


def test_a_training_day_reads_only_truths_settled_at_its_own_listing():
    # Training day 10-08 listed 10-06 05:00Z: the 10-05 truth (settled 10-06 03:00Z
    # in UTC) counts, 10-06 (settled 10-07) does not, however far off it is.
    rows = [(date(2026, 10, d), {A: 20.0}, 21.0 if d < 6 else 40.0, F._settled_at(date(2026, 10, d), "UTC"))
            for d in (2, 3, 4, 5, 6)]
    base = F._base_f({A: 20.0}, rows, event_date=date(2026, 10, 8), fleet_c=0.0,
                     knowable_by=F._listing_dt(date(2026, 10, 8)))
    assert base["biases"][A] == pytest.approx(1.0)
    live = F._base_f({A: 20.0}, rows, event_date=date(2026, 10, 8), fleet_c=0.0, knowable_by=None)
    assert live["biases"][A] > 1.0, "live, every settled truth up to event-2 counts (unchanged rule)"


def test_f_is_recalibrated_once_the_training_window_is_deep_enough(monkeypatch):
    days = [date(2026, 9, 16) + timedelta(days=i) for i in range(25)]
    history = {d: {A: 20.0, B: 21.0, C: 22.0} for d in days}
    stations = [f"S{i:03d}" for i in range(30)]
    truths = {(s, d.isoformat()): 22.0 for s in stations for d in days}
    _patch(monkeypatch, today={A: 20.0, B: 21.0, C: 22.0}, history=history, truths=truths)
    out = F.compute(_conn(), now=NOW, roster=[(s, 0.0, 0.0, "UTC") for s in stations])
    model = out["recalibration"]
    assert model["state"] == "FITTED" and model["rows"] == 750 and model["days"] == 25
    assert model["features"] == list(F.RECAL_FEATURES)
    entry = out["station_days"]["S000|2026-10-13"]
    assert entry["f_base_c"] == pytest.approx(21.0 + 1.0 + F.LAMBDA * 1.0)
    assert entry["recal_features"] == {"station_corr_c": 1.0, "members_skew_c": 0.0, "obs_to_f_change_c": 0.5}
    assert entry["f_c"] == pytest.approx(entry["f_base_c"] + entry["recal_c"], abs=1e-3)
    # F_base sat 0.5 C warm of every settled day; the correction pulls F toward the truth.
    assert abs(22.0 - entry["f_c"]) < abs(22.0 - entry["f_base_c"])
    assert out["rule"] == F.RULE == "deterministic_median_fleet_shift_station_share_truth_recal_v3"


# --------------------------------------------------------------------------
# 补记六: F_v3 on station values (user 「加」 10-12)
# --------------------------------------------------------------------------

def _terrain(offsets):
    return {"schema": F.terrain.SCHEMA, "built_at_utc": "2026-10-12T13:00:00Z",
            "systems": {system: {"EGLC": {"offset_c": off}} for system, off in offsets.items()}}


def _f_systems():
    ids = sorted(F.F_SYSTEM_IDS)
    return ids[0], ids[1], F.GFS_SYSTEM_ID


def test_station_values_add_each_members_lapse_offset_and_prefer_the_flux_grid_gfs():
    s1, s2, gfs = _f_systems()
    maxima = {s1: {"tmax_c": 20.0}, s2: {"tmax_c": 21.0}, gfs: {"tmax_c": 19.0}, F.GFS_SFLUX_SYSTEM_ID: {"tmax_c": 19.6}}
    table = _terrain({s1: 0.5, s2: -0.2, gfs: 0.3, F.GFS_SFLUX_SYSTEM_ID: 0.1})
    values = F.station_values(maxima, station_id="EGLC", table=table)
    # The GFS member is the flux-grid run plus the flux grid's own offset, keyed by the F member id.
    assert values == {s1: 20.5, s2: 20.8, gfs: pytest.approx(19.7)}
    # Without the flux run, the 0.25 degree run plus its own offset.
    del maxima[F.GFS_SFLUX_SYSTEM_ID]
    assert F.station_values(maxima, station_id="EGLC", table=table)[gfs] == pytest.approx(19.3)
    # A member the table has no cell for is left out, never used unadjusted (fail closed).
    assert s2 not in F.station_values(maxima, station_id="EGLC", table=_terrain({s1: 0.5, gfs: 0.3}))
    assert F.station_values(maxima, station_id="KLGA", table=table) == {}


def test_f_v3_is_written_beside_f_with_its_own_shift_share_and_recentring(monkeypatch):
    s1, s2, gfs = _f_systems()
    history = {date(2026, 10, d): {s1: 20.0, s2: 21.0, gfs: 22.0} for d in (1, 2, 3, 4)}
    truths = {("EGLC", f"2026-10-0{d}"): 22.5 for d in (1, 2, 3, 4)}
    table = _terrain({s1: 1.0, s2: 1.0, gfs: 1.0})
    _patch(monkeypatch, today={s1: 18.0, s2: 19.0, gfs: 21.0}, history=history, truths=truths, terrain_table=table)
    out = F.compute(_conn(), now=NOW, roster=[("EGLC", 51.5, 0.05, "Europe/London")])
    entry = out["station_days"]["EGLC|2026-10-13"]
    assert out["v3"]["rule"] == F.V3_RULE and out["v3"]["lambda"] == 0.9 and out["v3"]["state"] == "READY"
    v3 = entry["v3"]
    assert v3["state"] == "READY" and v3["gfs_source"] == gfs
    # Station values: every member + 1.0 -> raw median 20.0 (F's is 19.0).
    assert v3["raw_median_c"] == pytest.approx(20.0) and entry["raw_median_c"] == pytest.approx(19.0)
    # Biases on station values: history 21/22/23 vs truth 22.5 -> +1.5/+0.5/-0.5; today's 19/20/22
    # corrected 20.5/20.5/21.5 -> median 20.5 -> corr 0.5 (F's own, on grid values, is 1.5).
    assert v3["station_corr_c"] == pytest.approx(0.5)
    assert v3["f_c"] == pytest.approx(20.0 + F.RESEARCH_FLEET_C + 0.9 * 0.5)
    # Four days: no recalibration -> no re-centred number, and the cluster-like f_c stands.
    assert v3["recal_c"] is None and v3["f_recal_c"] is None
    assert out["v3"]["recalibration"]["state"] == "INSUFFICIENT_HISTORY"
    # F itself is untouched by the table.
    assert entry["f_c"] == pytest.approx(19.0 + F.RESEARCH_FLEET_C + F.LAMBDA * 1.5)


def test_f_v3_needs_three_station_values(monkeypatch):
    s1, s2, gfs = _f_systems()
    _patch(monkeypatch, today={s1: 18.0, s2: 19.0, gfs: 21.0}, history={}, truths={}, terrain_table=_terrain({s1: 0.0, gfs: 0.0}))
    out = F.compute(_conn(), now=NOW, roster=[("EGLC", 51.5, 0.05, "Europe/London")])
    assert out["station_days"]["EGLC|2026-10-13"]["v3"]["state"] == "TOO_FEW_SYSTEMS"


def test_the_flux_grid_gfs_is_registered_and_read_as_tmax():
    assert "GFS_SFLUX" in N.SPECS and N.SPECS["GFS_SFLUX"].mask == "gfs_sflux_land"
    assert N.step_url("GFS_SFLUX", RUN, 45).endswith("/gfs.20261011/00/atmos/gfs.t00z.sfluxgrbf045.grib2")
    assert N.index_url("GFS_SFLUX", RUN, 45).endswith("sfluxgrbf045.grib2.idx")
    assert N.FIELD_2T["GFS_SFLUX"] == "TMAX:2 m above ground"
    assert L.SYSTEM_IDS["GFS_SFLUX"] == "NOAA_GFS_SFLUX_13KM_NATIVE_POINT"
    assert L.SYSTEM_IDS["GFS_SFLUX"] in FAM.F_V3_SYSTEM_IDS and L.SYSTEM_IDS["GFS_SFLUX"] not in FAM.F_SYSTEM_IDS
    assert "GFS_SFLUX" in F.terrain.PRODUCTS


def test_terrain_table_read_fails_closed_on_a_foreign_file(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"schema": "something_else", "systems": {}}))
    assert F.terrain.read(path) is None
    path.write_text("not json")
    assert F.terrain.read(path) is None
    path.write_text(json.dumps({"schema": F.terrain.SCHEMA, "systems": {"X": {"EGLC": {"offset_c": 0.25}}}}))
    table = F.terrain.read(path)
    assert F.terrain.offset_c(table, system_id="X", station_id="EGLC") == 0.25
    assert F.terrain.offset_c(table, system_id="X", station_id="KLGA") is None


def test_bucket_for_reads_the_ladder_bounds():
    ladder = [
        {"label": "<=17", "lower_c": None, "upper_c": 17.5},
        {"label": "18", "lower_c": 17.5, "upper_c": 18.5},
        {"label": ">=19", "lower_c": 18.5, "upper_c": None},
    ]
    assert F.bucket_for(17.49, ladder) == "<=17"
    assert F.bucket_for(17.5, ladder) == "18"
    assert F.bucket_for(30.0, ladder) == ">=19"


def test_refresh_writes_the_runtime_file_and_never_the_ssot(tmp_path, monkeypatch):
    db = tmp_path / "ssot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE calibration_regional_point_run(x)")
    conn.commit()
    conn.close()
    target = tmp_path / "f.json"
    monkeypatch.setattr(F, "F_PATH", target)
    monkeypatch.setattr(F, "compute", lambda c: {"station_days": {"X|d": {"state": "READY"}},
                                                  "fleet": {"c": 0.8, "source": "research_fallback"},
                                                  "recalibration": {"state": "INSUFFICIENT_HISTORY"}})
    monkeypatch.setattr(F, "write", lambda payload, path=None: target.write_text("{}"))
    receipt = F.refresh(db)
    assert receipt["ready"] == 1 and target.exists()
    assert receipt["recalibration"] == "INSUFFICIENT_HISTORY"


# --------------------------------------------------------------------------
# 补记二: ICON global and ARPEGE
# --------------------------------------------------------------------------

def test_icon_and_arpege_object_addresses():
    assert N.step_url("ICON_GLOBAL", RUN, 36).endswith(
        "/icon/grib/00/t_2m/icon_global_icosahedral_single-level_2026101100_036_T_2M.grib2.bz2"
    )
    assert N.icon_invariant_url(RUN, "fr_land").endswith(
        "/icon/grib/00/fr_land/icon_global_icosahedral_time-invariant_2026101100_FR_LAND.grib2.bz2"
    )
    assert N.step_url("ARPEGE", RUN, 51).endswith(
        "/pnt/2026-10-11T00:00:00Z/arpege/025/SP1/arpege__025__SP1__049H072H__2026-10-11T00:00:00Z.grib2"
    )
    assert [N.arpege_group(h) for h in (0, 24, 25, 48, 49, 72, 73, 96)] == [
        "000H024H", "000H024H", "025H048H", "025H048H", "049H072H", "049H072H", "073H102H", "073H102H"]
    assert N.index_url("ICON_GLOBAL", RUN, 3) is None and N.index_url("ARPEGE", RUN, 3) is None


def _message(identity, length, *, unit=1):
    """A synthetic GRIB2 message: sections 0, 1, 3 and 4, padded to ``length``."""

    discipline, category, number, template, surface, value, hours = identity
    s1 = struct.pack(">IB", 21, 1) + bytes(16)
    s3 = struct.pack(">IB", 72, 3) + bytes(67)
    s4 = bytearray(34)
    s4[0:5] = struct.pack(">IB", 34, 4)
    s4[7:9] = struct.pack(">H", template)
    s4[9], s4[10] = category, number
    s4[17] = unit
    s4[18:22] = struct.pack(">I", hours)
    s4[22] = surface
    s4[24:28] = struct.pack(">I", value)
    head = b"GRIB" + bytes(2) + bytes([discipline, 2]) + struct.pack(">Q", length)
    body = head + s1 + s3 + bytes(s4)
    return body + bytes(length - len(body) - 4) + b"7777"


T2M = lambda h: (0, 0, 0, 0, 103, 2, h)  # noqa: E731
WIND = lambda h: (0, 2, 2, 0, 103, 10, h)  # noqa: E731


def test_grib2_header_reads_identity_and_length():
    msg = _message(T2M(36), 600)
    assert N.parse_grib2_header(msg[:256]) == (600, T2M(36))
    assert N.parse_grib2_header(_message(T2M(90), 600, unit=0)[:256])[1][6] == 1.5, "minutes are hours / 60"
    assert N.parse_grib2_header(b"GRIB" + bytes(12)) is None, "length 0 is not a message"
    assert N.parse_grib2_header(b"XXXX" + bytes(252)) is None


def _package(identities, sizes):
    return b"".join(_message(i, n) for i, n in zip(identities, sizes))


def _fake_get(blob, reads=None):
    def get(url, headers=None, *, cap):
        lo, hi = headers["Range"].split("=")[1].split("-")
        chunk = blob[int(lo):min(len(blob), int(hi) + 1)]
        assert len(chunk) <= cap
        if reads is not None:
            reads.append(len(chunk))
        return chunk
    return get


def test_arpege_table_walks_segments_in_parallel_and_proves_contiguity(monkeypatch):
    monkeypatch.setattr(N, "ARPEGE_SEGMENT_BYTES", 2000)
    monkeypatch.setattr(N, "ARPEGE_RESYNC_BYTES", 700)
    ids = [WIND(h) for h in range(6)] + [T2M(h) for h in range(6)]
    sizes = [500 + 37 * k for k in range(12)]
    blob = _package(ids, sizes)
    reads = []
    table = N.arpege_message_table("u", size=len(blob), get=_fake_get(blob, reads), workers=4)
    assert [t[2] for t in table] == ids
    assert [t[1] for t in table] == sizes
    assert sum(reads) < len(blob), "headers and resync windows, never the whole package"


def test_a_false_header_inside_a_message_fails_the_package_closed(monkeypatch):
    monkeypatch.setattr(N, "ARPEGE_SEGMENT_BYTES", 1000)
    monkeypatch.setattr(N, "ARPEGE_RESYNC_BYTES", 400)
    good = _package([WIND(0), T2M(0), T2M(1)], [1500, 900, 900])
    # Plant a plausible header in the first message's payload, inside the
    # second segment's resync window.
    fake = b"GRIB" + bytes(2) + bytes([0, 2]) + struct.pack(">Q", 333)
    blob = good[:1100] + fake + good[1100 + len(fake):]
    with pytest.raises(Exception, match="arpege"):
        N.arpege_message_table("u", size=len(blob), get=_fake_get(blob), workers=3)


def test_arpege_reads_only_the_2m_message_of_the_step(monkeypatch):
    url = N.step_url("ARPEGE", RUN, 12)
    monkeypatch.setitem(N._ARPEGE_TABLES, url, [(0, 100, WIND(12)), (100, 50, T2M(11)), (150, 70, T2M(12))])
    calls = []
    monkeypatch.setattr(N, "_get", lambda u, headers=None, *, cap: calls.append((u, headers)) or b"m")
    assert N.read_message("ARPEGE", RUN, 12) == b"m"
    assert calls == [(url, {"Range": "bytes=150-219"})]
    assert N.read_message("ARPEGE", RUN, 13) is None, "a step the table lacks is not read"


def test_icon_bz2_message_is_decompressed_within_its_cap(monkeypatch):
    monkeypatch.setattr(N, "_get", lambda u, headers=None, *, cap: bz2.compress(b"G" * 1000))
    assert N.read_message("ICON_GLOBAL", RUN, 3) == b"G" * 1000
    monkeypatch.setattr(N, "ICON_MAX_MESSAGE_BYTES", 100)
    with pytest.raises(Exception, match="over_cap"):
        N.read_message("ICON_GLOBAL", RUN, 3)


def test_icon_cells_follow_the_land_rule_on_an_unstructured_grid():
    # Cells on the equator: 0-2 about 11 km apart (water, land, land); 3-4
    # about 5.6 km apart far east, both water.
    lats = [0.0, 0.0, 0.0, 0.0, 0.0]
    lons = [0.0, 0.1, 0.2, 5.0, 5.05]
    land = [0.0, 0.9, 0.9, 0.0, 0.2]
    placed = N.place_on_cells({"A": (0.0, 0.2), "B": (0.0, 0.0), "C": (0.0, 5.0)},
                              latitudes=lats, longitudes=lons, land=land)
    assert placed["A"]["index"] == 2 and placed["A"]["moved"] is False
    assert placed["B"]["index"] == 1 and placed["B"]["moved"] is True, "nearest is water: the nearest land cell in reach"
    assert placed["B"]["distance_km"] == pytest.approx(11.1, abs=0.1)
    assert placed["C"] is None, "no land cell within reach: left out, never guessed"


def test_a_coastal_station_two_spacings_from_land_is_placed():
    """2026-10-13: RKSI sat 19 km from land on ICON's ~10 km grid, past the
    old 1.5-spacing reach (47/48).  The reach is 2.5 spacings now; a station
    with land nearer is unchanged (still the nearest land cell)."""

    # 10 km spacing on the equator: water at 0.0/0.09, land at 0.18 (~20 km).
    lats = [0.0, 0.0, 0.0]
    lons = [0.0, 0.09, 0.18]
    land = [0.0, 0.0, 0.9]
    placed = N.place_on_cells({"RKSI": (0.0, 0.0)}, latitudes=lats, longitudes=lons, land=land)
    assert placed["RKSI"]["index"] == 2 and placed["RKSI"]["moved"] is True
    assert placed["RKSI"]["distance_km"] == pytest.approx(20.0, abs=0.1)


def test_a_cached_none_is_placed_again(tmp_path):
    import json

    import numpy as np

    grid = {"CLAT": [0.0, 0.0], "CLON": [0.0, 0.1], "FR_LAND": [0.0, 1.0]}
    decode = lambda m: np.asarray(grid[m.decode()])  # noqa: E731
    reader = lambda url: url.rsplit("/", 1)[1].split("_", 5)[5].split(".")[0].encode()  # noqa: E731
    folder = tmp_path / "2"
    folder.mkdir()
    (folder / "station_cells.json").write_text(json.dumps({"A|0.00000|0.00000": None}))
    from unittest import mock

    with mock.patch.object(N, "ICON_GRID_POINT_COUNTS", frozenset({2})):
        cells = N.icon_station_cells(RUN, {"A": (0.0, 0.0)}, point_count=2, cache_dir=tmp_path,
                                     reader=reader, decode=decode)
    assert cells["A"]["index"] == 1


def test_icon_cells_are_cached_by_the_runs_own_grid_size(tmp_path):
    import numpy as np

    grid = {"CLAT": [0.0, 0.0, 0.0], "CLON": [0.0, 0.1, 0.2], "FR_LAND": [1.0, 1.0, 1.0]}
    asked = []

    def reader(url):
        asked.append(url)
        return url.rsplit("/", 1)[1].split("_", 5)[5].split(".")[0].encode()

    decode = lambda m: np.asarray(grid[m.decode()])  # noqa: E731
    from unittest import mock

    with mock.patch.object(N, "ICON_GRID_POINT_COUNTS", frozenset({3})):
        cells = N.icon_station_cells(RUN, {"A": (0.0, 0.11)}, point_count=3, cache_dir=tmp_path,
                                     reader=reader, decode=decode)
        assert cells["A"]["index"] == 1 and len(asked) == 3
        again = N.icon_station_cells(RUN, {"A": (0.0, 0.11)}, point_count=3, cache_dir=tmp_path,
                                     reader=reader, decode=decode)
        assert again == cells and len(asked) == 3, "grid and placement come from the cache"
        assert (tmp_path / "3" / "CLAT.npy").exists()
        with pytest.raises(Exception, match="icon_grid_invalid"):
            N.icon_station_cells(RUN, {"A": (0.0, 0.11)}, point_count=4, cache_dir=tmp_path,
                                 reader=reader, decode=decode)


def test_icon_run_picks_each_station_cell_from_one_field_a_step():
    import numpy as np

    def reader(product, run_init, step):
        return str(step).encode()

    decode = lambda m: np.full(5, 273.15 + int(m.decode()) / 10.0)  # noqa: E731
    seen = {}

    def cells(run_init, points, *, point_count):
        seen["count"] = point_count
        return {"RJTT": {"index": 3}, "XSEA": None}

    out = N.fetch_icon_run_for_targets(run_init=RUN, targets=_targets(), reader=reader, cells=cells, decode=decode)
    assert seen["count"] == 5, "the grid is the one the run's own message declares"
    assert set(out) == {("RJTT", "2026-10-12"), ("RJTT", "2026-10-13")}
    assert out[("RJTT", "2026-10-12")]["hourly_c"][15] == pytest.approx(1.5)
    assert out[("RJTT", "2026-10-12")]["source_url"].endswith("#icon_cell=3")

    bad = lambda m: np.full(5 if m == b"15" else 6, 280.0)  # noqa: E731
    with pytest.raises(Exception, match="icon_grid_mismatch"):
        N.fetch_icon_run_for_targets(run_init=RUN, targets=_targets()[:1], reader=reader, cells=cells, decode=bad)


def test_capture_bounds_the_slow_native_products():
    capture = _load("capture_deterministic_forward")
    assert capture.MAX_RUNS_OVERRIDE == {"ARPEGE": 1, "ICON_GLOBAL": 1, "UKMO_GLOBAL": 1}
    assert max(capture.NATIVE_BUDGET_OVERRIDE_SEC.values()) <= 240.0


def test_gfs_hourly_reads_the_same_objects_at_every_hour():
    assert N.step_url("GFS_HOURLY", RUN, 7) == N.step_url("GFS", RUN, 7).replace("f007", "f007")
    assert N.SPECS["GFS_HOURLY"].steps()[:4] == [0, 1, 2, 3] and N.SPECS["GFS_HOURLY"].steps()[-1] == 96
    assert FAM.FAMILY["GFS_HOURLY"]["step_hours"] == 1


# --------------------------------------------------------------------------
# 补记三: UKMO global (HDF5 chunks) and the production-network yield
# --------------------------------------------------------------------------

def test_ukmo_objects_are_period_maxima_hourly_then_three_hourly():
    assert N.step_url("UKMO_GLOBAL", RUN, 36).endswith(
        "/global-deterministic-10km/20261011T0000Z/20261012T1200Z-PT0036H00M-temperature_at_screen_level_max-PT01H.nc"
    )
    assert N.step_url("UKMO_GLOBAL", RUN, 60).endswith("-PT0060H00M-temperature_at_screen_level_max-PT03H.nc")
    steps = N.SPECS["UKMO_GLOBAL"].steps()
    assert steps[:3] == [1, 2, 3] and 54 in steps and 55 not in steps and steps[-2:] == [93, 96]
    assert N.run_directory_url("UKMO_GLOBAL", RUN).endswith("/global-deterministic-10km/20261011T0000Z/")
    with pytest.raises(Exception, match="values_not_messages"):
        N.read_message("UKMO_GLOBAL", RUN, 36)


def test_cached_range_file_prefetches_in_parallel_and_records_what_it_read():
    blob = bytes(range(256)) * 40  # 10240 bytes
    asked = []

    def get(url, headers=None, *, cap):
        lo, hi = (int(x) for x in headers["Range"].split("=")[1].split("-"))
        asked.append(lo)
        return blob[lo:hi + 1]

    handle = N.CachedRangeFile("u", get=get, size=len(blob), first=blob[:1024], block=1024)
    handle.prefetch([0, 3, -1])
    assert sorted(asked) == [3072, 9216], "block 0 came with the first read; -1 is the last block"
    handle.seek(3000)
    assert handle.read(200) == blob[3000:3200]
    assert handle.touched == [2, 3] and sorted(asked) == [2048, 3072, 9216]
    handle.seek(-10, 2)
    assert handle.read() == blob[-10:]


def test_cached_range_file_caps_the_metadata_a_file_may_pull(monkeypatch):
    monkeypatch.setattr(N, "UKMO_MAX_FILE_BLOCKS", 2)
    handle = N.CachedRangeFile("u", get=lambda u, headers=None, *, cap: bytes(10), size=100, first=bytes(10), block=10)
    handle.seek(10); handle.read(20)
    with pytest.raises(Exception, match="metadata_over_cap"):
        handle.seek(50); handle.read(5)


def test_ukmo_run_curves_from_period_maxima_on_gfs_land(monkeypatch):
    seen = []

    def step_values(step, cells):
        seen.append(step)
        return {s: 273.15 + step / 10.0 for s in cells}

    def land(mask, points):
        return [{"latitude": lat, "longitude": lon, "moved": False} if lon > 0 else None for lat, lon in points]

    out = N.fetch_ukmo_run_for_targets(run_init=RUN, targets=_targets(), step_values=step_values,
                                       mask_reader=lambda p, r: b"mask", land=land)
    assert set(out) == {("RJTT", "2026-10-12"), ("RJTT", "2026-10-13")}
    day = out[("RJTT", "2026-10-12")]
    # UTC+9: local 10-12 = 10-11 15Z .. 10-12 15Z -> hourly maxima ending 15..38.
    assert sorted(day["hourly_c"]) == list(range(15, 39))
    assert day["hourly_c"][15] == pytest.approx(1.5)
    assert seen[0] == min(seen) and len(seen) == len(set(seen)), "each file once, the first alone (it teaches the prefetch)"


def test_heavy_products_yield_to_a_strained_production_network(monkeypatch):
    import bayes_basket_target.supply_watch as watch

    monkeypatch.setattr(watch, "egress_send_gate", lambda: {"watch": "fresh", "egress_health": "amber"})
    assert N.production_network_strained() == "amber"
    monkeypatch.setattr(watch, "egress_send_gate", lambda: {"watch": "stale", "egress_health": "red"})
    assert N.production_network_strained() == "", "a stale watch holds nothing back"
    monkeypatch.setattr(watch, "egress_send_gate", lambda: {"watch": "fresh", "egress_health": "green"})
    assert N.production_network_strained() == ""
    assert N.HEAVY_PRODUCTS == {"ICON_GLOBAL", "ARPEGE", "UKMO_GLOBAL"}


def test_ukmo_reads_real_hdf5_chunks_when_h5py_is_present(tmp_path):
    h5py = pytest.importorskip("h5py")
    import numpy as np

    path = tmp_path / "f.nc"
    lat = np.linspace(-89.5, 89.5, 180).astype("f4")
    lon = np.linspace(-179.5, 179.5, 360).astype("f4")
    field = (np.arange(180 * 360, dtype="f4").reshape(180, 360) % 50) + 250.0
    with h5py.File(path, "w") as h:
        h["latitude"] = lat
        h["longitude"] = lon
        h.create_dataset("air_temperature", data=field, chunks=(32, 32), compression="gzip")
    blob = path.read_bytes()

    def get(url, headers=None, *, cap):
        lo, hi = (int(x) for x in headers["Range"].split("=")[1].split("-"))
        return blob[lo:hi + 1]

    from unittest import mock

    with mock.patch.object(N, "_ukmo_size", lambda url, fetch: len(blob)):
        got = N.ukmo_step_values("u", {"A": (10.2, 20.4), "B": (-45.0, 170.0)}, get=get)
    j, i = int(np.argmin(np.abs(lat - 10.2))), int(np.argmin(np.abs(lon - 20.4)))
    assert got["A"] == pytest.approx(float(field[j, i]))
    j, i = int(np.argmin(np.abs(lat + 45.0))), int(np.argmin(np.abs(lon - 170.0)))
    assert got["B"] == pytest.approx(float(field[j, i]))


def test_a_capture_pass_yields_before_a_runs_fetch_when_asked(monkeypatch):
    conn = sqlite3.connect(":memory:")
    L.ensure_schema(conn)
    item = {"run_init": RUN, "tier": 0, "targets": [dict(t, settlement_timezone="Asia/Tokyo") for t in _targets()[:1]]}
    monkeypatch.setattr(L, "plan_p1", lambda conn, **k: [item, dict(item, run_init=RUN.replace(hour=12))])
    fetched = []
    monkeypatch.setattr(N, "fetch_run_for_targets", lambda **k: fetched.append(k["run_init"]) or {})
    answers = iter(["", "amber"])
    receipt = L.capture_p1(conn, product="GFS", now=RUN, stations=[], max_runs=4,
                           should_yield=lambda: next(answers))
    assert fetched == [RUN], "the first run was fetched; the second was not started"
    assert receipt["yielded"] == "amber" and receipt["runs_attempted"] == 1
