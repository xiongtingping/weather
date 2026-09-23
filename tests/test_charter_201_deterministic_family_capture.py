"""Charter 201, step 1: the deterministic-forecast family is captured, P1 is not moved.

User ruling 2026-09-23: METAR is observation, the regional / high-resolution /
AI single runs are forecasts -- features, never ensemble members -- captured
every run as each source publishes it, for every listed target day, newest
wins.  This step captures and stores; the served surface must not move:

* P1 reads exactly the (system, station) pairs it was fitted on
  (``regional_features.P1_FEATURE_PAIRS``), whatever else the lane stores;
* the P1 lane's own reach (products, days, runs) is planned first and capped
  as before, the wider reach after it with its own cap;
* the relays are charged to one daily Open-Meteo budget and stop dead on the
  aggregator's "daily limit" until the next UTC midnight.

No network: transports and decoders are faked.
"""

from __future__ import annotations

import bz2
import importlib.util
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import aifs_single_point_archive as AIFS  # noqa: E402
from bayes_basket_target import cma_grapes_point_archive as CMA  # noqa: E402
from bayes_basket_target import deterministic_forecast_family as FAM  # noqa: E402
from bayes_basket_target import dwd_icon_eu_point_archive as ICON  # noqa: E402
from bayes_basket_target import open_meteo_quota as Q  # noqa: E402
from bayes_basket_target import open_meteo_relay_archive as RELAY  # noqa: E402
from bayes_basket_target import regional_features as R  # noqa: E402
from bayes_basket_target import regional_point_lane as L  # noqa: E402


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(Q, "LEDGER_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(CMA, "_SERIES_CACHE", {})
    yield


# ---------------------------------------------------------------------------
# P1 reads the pairs it was fitted on, and nothing the family adds
# ---------------------------------------------------------------------------

def _curve_row(system, run="2026-09-23T00:00:00Z", peak=33.0):
    return {
        "ensemble_system_id": system, "run_init_utc": run,
        "knowable_at_utc": "2026-09-23T01:00:00Z", "lead_hours_to_local_day_start": -2.0,
        "forecast_tmax_c": peak,
        "hourly_c": {"2026-09-23T05:00:00Z": peak - 1, "2026-09-23T06:00:00Z": peak},
    }


def test_p1_never_reads_a_pair_it_was_not_fitted_on():
    kw = dict(as_of_utc="2026-09-23T04:00:00Z", running_high_c=30.0, latest_observed_c=29.0)
    # A newer AIFS run and CMA at a station P1 never had CMA for: both ignored.
    rows = [
        _curve_row("ECMWF_AIFS_SINGLE_POINT", run="2026-09-23T01:00:00Z", peak=40.0),
        _curve_row("CMA_GRAPES_GLOBAL_POINT", run="2026-09-23T00:30:00Z", peak=39.0),
        _curve_row("NCEP_NBM_POINT", peak=33.0),
    ]
    out = R.regional_features_at(rows, station_id="KSEA", **kw)
    assert out["regional_system_id"] == "NCEP_NBM_POINT"
    assert out["regional_headroom_c"] == pytest.approx(3.0)
    # The same rows read exactly as before the family existed.
    legacy = R.regional_features_at(rows[2:], station_id="KSEA", **kw)
    assert out == legacy
    # And CMA at a station P1 was fitted with it is still read.
    zuuu = R.regional_features_at([rows[1]], station_id="ZUUU", **kw)
    assert zuuu["regional_system_id"] == "CMA_GRAPES_GLOBAL_POINT"


def test_the_frozen_pairs_are_the_five_lanes_and_their_rosters():
    assert R.P1_FEATURE_SYSTEMS == {
        "JMA_MSM_POINT", "CMA_GRAPES_GLOBAL_POINT", "DWD_ICON_EU_POINT",
        "NCEP_HRRR_POINT", "NCEP_NBM_POINT",
    }
    from bayes_basket_target import jma_msm_archive, noaa_conus_archive  # noqa: PLC0415

    for system, roster in (
        ("JMA_MSM_POINT", jma_msm_archive.CAPTURE_STATIONS),
        ("CMA_GRAPES_GLOBAL_POINT", CMA.CAPTURE_STATIONS),
        ("DWD_ICON_EU_POINT", ICON.CAPTURE_STATIONS),
        ("NCEP_HRRR_POINT", noaa_conus_archive.CAPTURE_STATIONS["HRRR"]),
        ("NCEP_NBM_POINT", noaa_conus_archive.CAPTURE_STATIONS["NBM"]),
    ):
        assert {s for sys_, s in R.P1_FEATURE_PAIRS if sys_ == system} == {s[0] for s in roster}
    assert not (R.P1_FEATURE_SYSTEMS & {
        L.SYSTEM_IDS["AIFS"], L.SYSTEM_IDS["IFS9KM"], L.SYSTEM_IDS["UKMO10"],
    })


# ---------------------------------------------------------------------------
# the plan: P1's own reach first, exactly as before; the wider reach after
# ---------------------------------------------------------------------------

def _legacy_runs(product, now, event_date, zone):
    """The P1 lane's reach as it was written before charter 201."""

    from zoneinfo import ZoneInfo  # noqa: PLC0415

    day_start = datetime.combine(event_date, datetime.min.time(), tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    latest = now - timedelta(hours=L.PUBLICATION_LAG_HOURS[product])
    first = (day_start - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    oldest = now - timedelta(hours=24) if product == "CMA" else first
    out, stamp = [], first
    while stamp < day_end:
        if stamp.hour in L.RUN_CYCLE_HOURS[product] and oldest <= stamp <= latest:
            out.append(stamp)
        stamp += timedelta(hours=1)
    return out


@pytest.mark.parametrize("product,zone", [
    ("HRRR", "America/New_York"), ("NBM", "America/Los_Angeles"),
    ("ICON_EU", "Europe/Berlin"), ("MSM", "Asia/Tokyo"), ("CMA", "Asia/Shanghai"),
])
def test_the_p1_reach_is_tier_zero_and_unchanged(product, zone):
    now = datetime(2026, 9, 23, 3, 30, tzinfo=timezone.utc)
    for offset in (-1, 0, 1):
        day = (now + timedelta(days=offset)).date()
        tiers = L._candidate_run_tiers(product, now, day, zone, legacy=True)
        assert sorted(r for r, t in tiers.items() if t == 0) == _legacy_runs(product, now, day, zone)
        for run, tier in tiers.items():
            if tier == 1:
                assert run >= now - timedelta(hours=24)


def test_the_wider_reach_takes_d_plus_2_and_only_runs_that_reach_its_afternoon():
    now = datetime(2026, 9, 23, 3, 30, tzinfo=timezone.utc)
    d2 = date(2026, 9, 25)
    icon = L._candidate_run_tiers("ICON_EU", now, d2, "Europe/Berlin", legacy=False)
    assert icon and set(icon.values()) == {1}
    # 03/09/15/21Z ICON-EU runs stop at 30 h and cannot reach D+2's afternoon.
    assert all(run.hour in (0, 6, 12, 18) for run in icon)
    hrrr = L._candidate_run_tiers("HRRR", now, d2, "America/New_York", legacy=False)
    assert hrrr == {}, "HRRR's 48 h never reaches D+2's afternoon from a current run"
    aifs = L._candidate_run_tiers("AIFS", now, d2, "America/Sao_Paulo", legacy=True)
    assert aifs and set(aifs.values()) == {1}, "a new product has no P1 reach"


def test_the_plan_puts_every_p1_run_before_the_wider_reach(monkeypatch):
    conn = sqlite3.connect(":memory:")
    L.ensure_schema(conn)
    now = datetime(2026, 9, 23, 3, 30, tzinfo=timezone.utc)
    plan = L.plan_p1(conn, product="ICON_EU", now=now,
                     stations=[("EDDM", 48.35, 11.79, "Europe/Berlin")], max_runs=2)
    tiers = [item["tier"] for item in plan]
    assert tiers == sorted(tiers) and 0 in tiers and 1 in tiers
    assert sum(1 for t in tiers if t == 0) <= 8 and sum(1 for t in tiers if t == 1) <= 8
    legacy = [item for item in plan if item["tier"] == 0]
    assert [i["run_init"] for i in legacy] == sorted((i["run_init"] for i in legacy), reverse=True)


def test_each_reach_has_its_own_cap_and_an_unpublished_run_is_asked_once(monkeypatch, tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    L.ensure_schema(conn)
    now = datetime(2026, 9, 23, 3, 30, tzinfo=timezone.utc)
    run_new, run_old = datetime(2026, 9, 22, 18, tzinfo=timezone.utc), datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    target = {"station_id": "EDDM", "latitude": 48.35, "longitude": 11.79, "event_date": date(2026, 9, 23),
              "utc_offset_hours": 2, "settlement_timezone": "Europe/Berlin"}
    far = dict(target, event_date=date(2026, 9, 25))
    plan = [
        {"product": "ICON_EU", "run_init": run_new, "targets": [target], "tier": 0},
        {"product": "ICON_EU", "run_init": run_old, "targets": [target], "tier": 0},
        {"product": "ICON_EU", "run_init": run_new, "targets": [far], "tier": 1},
        {"product": "ICON_EU", "run_init": run_old, "targets": [far], "tier": 1},
    ]
    monkeypatch.setattr(L, "plan_p1", lambda *a, **k: plan)
    asked: list[tuple[datetime, str]] = []

    def fake(*, run_init, targets):
        asked.append((run_init, targets[0]["event_date"].isoformat()))
        if run_init == run_new:
            return {}
        return {("EDDM", t["event_date"].isoformat()): {"hourly_c": {30: 20.0}, "hours_wanted": [30]} for t in targets}

    monkeypatch.setattr(ICON, "fetch_run_day_maxima", fake)
    receipt = L.capture_p1(conn, product="ICON_EU", now=now, stations=[], max_runs=1)
    assert asked == [(run_new, "2026-09-23"), (run_old, "2026-09-23"), (run_old, "2026-09-25")], (
        "the unpublished 18Z is not asked again for D+2; each reach takes its one productive run"
    )
    assert receipt["written"] == 2


# ---------------------------------------------------------------------------
# the relay: one budget, one backoff
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class _Client:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, params=None, headers=None):
        self.calls.append(params)
        return self.answers.pop(0)


RUN = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


def _series(start, hours, base):
    return {"hourly": {
        "time": [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)],
        "temperature_2m": [base + h * 0.1 for h in range(hours)],
    }}


def test_one_request_serves_every_station_and_is_charged_per_location(monkeypatch):
    client = _Client([_Response([_series(RUN, 96, 10.0), _series(RUN, 96, 20.0)])])
    monkeypatch.setattr(RELAY, "_client", lambda: client)
    targets = [
        {"station_id": "SBGR", "latitude": -23.4, "longitude": -46.5, "event_date": date(2026, 9, 23), "utc_offset_hours": -3},
        {"station_id": "FACT", "latitude": -34.0, "longitude": 18.6, "event_date": date(2026, 9, 23), "utc_offset_hours": 2},
        {"station_id": "FACT", "latitude": -34.0, "longitude": 18.6, "event_date": date(2026, 9, 24), "utc_offset_hours": 2},
    ]
    out = RELAY.fetch_relay_run_for_targets(product="IFS9KM", run_init=RUN, targets=targets)
    assert len(client.calls) == 1 and client.calls[0]["cell_selection"] == "land"
    assert client.calls[0]["run"] == "2026-09-22T12:00" and client.calls[0]["models"] == "ecmwf_ifs"
    assert Q.spent(RELAY.QUOTA_LANE) == 2, "two locations, one call each"
    # Stations are asked in sorted order: FACT is the first series.
    assert out[("FACT", "2026-09-23")]["hourly_c"][10] == pytest.approx(10.0 + 1.0)
    assert len(out[("SBGR", "2026-09-23")]["hourly_c"]) == 24


def test_an_unpublished_run_is_named_so(monkeypatch):
    client = _Client([_Response({"error": True, "reason": "The requested model run is not available."}, 400)])
    monkeypatch.setattr(RELAY, "_client", lambda: client)
    with pytest.raises(RELAY.RunNotPublished):
        RELAY.fetch_run_series("UKMO10", RUN, [(51.5, 0.0)])


def test_the_daily_limit_stops_every_relay_call_until_utc_midnight(monkeypatch):
    client = _Client([_Response({"error": True, "reason": "Daily API request limit exceeded. Please try again tomorrow."}, 429)])
    monkeypatch.setattr(RELAY, "_client", lambda: client)
    now = datetime(2026, 9, 23, 15, 51, tzinfo=timezone.utc)
    with pytest.raises(Q.QuotaRefused):
        RELAY.fetch_run_series("IFS9KM", RUN, [(51.5, 0.0)], now=now)
    assert Q.exhausted(now) and Q.exhausted(now.replace(hour=23, minute=59))
    assert not Q.exhausted(datetime(2026, 9, 24, 0, 0, 1, tzinfo=timezone.utc))
    # Nothing more is sent today -- CMA included.
    with pytest.raises(Q.QuotaRefused):
        Q.charge("deterministic_relay", 1, now)
    assert len(client.calls) == 1


def test_a_per_minute_refusal_is_waited_out(monkeypatch):
    client = _Client([
        _Response({"error": True, "reason": "Too many concurrent requests"}, 429),
        _Response(_series(RUN, 4, 5.0)),
    ])
    monkeypatch.setattr(RELAY, "_client", lambda: client)
    paused: list[int] = []
    monkeypatch.setattr(Q, "throttle_pause", lambda attempt: paused.append(attempt))
    out = RELAY.fetch_run_series("IFS9KM", RUN, [(51.5, 0.0)], forecast_hours=4)
    assert paused == [0] and len(out[0]) == 4 and len(client.calls) == 2


def test_a_spent_lane_budget_sends_nothing(monkeypatch):
    client = _Client([])
    monkeypatch.setattr(RELAY, "_client", lambda: client)
    monkeypatch.setitem(Q.DAILY_BUDGET, "deterministic_relay", 3)
    Q.charge("deterministic_relay", 2)
    with pytest.raises(Q.QuotaRefused, match="lane_budget_spent"):
        RELAY.fetch_run_series("IFS9KM", RUN, [(1.0, 1.0), (2.0, 2.0)])
    assert client.calls == []


def test_cma_is_charged_and_asks_a_run_once_per_station_per_pass(monkeypatch):
    client = _Client([_Response(_series(RUN, 79, 25.0))])
    monkeypatch.setattr(CMA, "_client", lambda: client)
    for day in (date(2026, 9, 23), date(2026, 9, 24)):
        CMA.fetch_day_maximum(run_init=RUN, event_date=day, utc_offset_hours=8,
                              latitude=30.576, longitude=103.95)
    assert len(client.calls) == 1 and Q.spent(CMA.QUOTA_LANE) == 1


# ---------------------------------------------------------------------------
# AIFS Single: the land rule off its own lsm, one field at a time
# ---------------------------------------------------------------------------

def test_aifs_reads_every_target_at_its_land_cell_and_skips_what_it_cannot_place(monkeypatch):
    asked: list[tuple[int, str]] = []

    def message(run_init, step, param):
        asked.append((step, param))
        return f"{param}@{step}".encode()

    monkeypatch.setattr(AIFS, "_message", message)
    monkeypatch.setattr(AIFS, "nearest_land_points", lambda mask, points: [
        {"latitude": -34.0, "longitude": 18.75, "moved": True} if abs(lon - 18.602) < 1e-6
        else None if abs(lat) < 1e-6 else {"latitude": lat, "longitude": lon, "moved": False}
        for lat, lon in points
    ])
    decoded: list[tuple[bytes, float, float]] = []

    def nearest(msg, *, latitude, longitude):
        decoded.append((msg, latitude, longitude))
        return 293.15 + int(msg.split(b"@")[1]) / 100.0, 1.0

    monkeypatch.setattr(AIFS, "nearest_point_kelvin", nearest)
    targets = [
        {"station_id": "FACT", "latitude": -33.965, "longitude": 18.602, "event_date": date(2026, 9, 23), "utc_offset_hours": 2},
        {"station_id": "SBGR", "latitude": -23.432, "longitude": -46.469, "event_date": date(2026, 9, 23), "utc_offset_hours": -3},
        {"station_id": "XSEA", "latitude": 0.0, "longitude": -150.0, "event_date": date(2026, 9, 23), "utc_offset_hours": -10},
    ]
    out = AIFS.fetch_run_for_targets(run_init=RUN, targets=targets)
    assert asked[0] == (0, "lsm") and all(p == "2t" for _s, p in asked[1:])
    assert ("XSEA", "2026-09-23") not in out
    assert out[("FACT", "2026-09-23")]["source_url"].endswith("#land_cell=-34.00000,18.75000")
    assert "#" not in out[("SBGR", "2026-09-23")]["source_url"]
    fact_steps = AIFS.local_day_steps(run_init=RUN, event_date=date(2026, 9, 23), utc_offset_hours=2)
    assert sorted(out[("FACT", "2026-09-23")]["hourly_c"]) == fact_steps
    assert all(lat == -34.0 for msg, lat, lon in decoded if lon == 18.75)


def test_aifs_without_its_mask_reads_nothing(monkeypatch):
    monkeypatch.setattr(AIFS, "_message", lambda run_init, step, param: None if param == "lsm" else b"x")
    targets = [{"station_id": "SBGR", "latitude": -23.4, "longitude": -46.5, "event_date": date(2026, 9, 23), "utc_offset_hours": -3}]
    assert AIFS.fetch_run_for_targets(run_init=RUN, targets=targets) == {}


# ---------------------------------------------------------------------------
# ICON-EU: charter 200's rule off FR_LAND, streamed
# ---------------------------------------------------------------------------

def test_icon_eu_streams_and_reads_land_stations_where_they_stand(monkeypatch, tmp_path):
    monkeypatch.setattr(ICON, "LAND_MASK_CACHE", tmp_path / "fr_land.grib2")
    (tmp_path / "fr_land.grib2").write_bytes(b"FRLAND")
    placed: list[bytes] = []

    def land(mask, points):
        placed.append(mask)
        return [{"latitude": lat, "longitude": lon, "moved": False} for lat, lon in points]

    monkeypatch.setattr(ICON, "nearest_land_points", land)
    monkeypatch.setattr(ICON, "_get", lambda url: bz2.compress(url.encode()))
    seen: list[tuple[float, float]] = []

    def nearest(message, *, latitude, longitude):
        seen.append((latitude, longitude))
        return 290.0, 1.0

    monkeypatch.setattr(ICON, "nearest_point_kelvin", nearest)
    run = datetime(2026, 9, 22, 18, tzinfo=timezone.utc)
    targets = [{"station_id": s, "latitude": la, "longitude": lo, "event_date": date(2026, 9, 23), "utc_offset_hours": 2}
               for s, la, lo, _z in ICON.CAPTURE_STATIONS[:2]]
    out = ICON.fetch_run_day_maxima(run_init=run, targets=targets)
    assert placed == [b"FRLAND"], "the cached mask, read once"
    assert set(seen) == {(t["latitude"], t["longitude"]) for t in targets}, "their own coordinates"
    assert all(v["hourly_c"] and v["forecast_tmax_c"] == pytest.approx(290.0 - 273.15) for v in out.values())


# ---------------------------------------------------------------------------
# the as-of read: newest complete run, never older than it could already read
# ---------------------------------------------------------------------------

def _day_curve(event_date, zone, start_hour, values):
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    start = datetime.combine(event_date, datetime.min.time(), tzinfo=ZoneInfo(zone))
    return {
        (start + timedelta(hours=start_hour + i)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"): v
        for i, v in enumerate(values)
    }


def test_day_maximum_needs_the_whole_afternoon():
    day = date(2026, 9, 23)
    full = _day_curve(day, "America/Sao_Paulo", 0, [15.0 + (i if i < 15 else 30 - i) * 0.5 for i in range(24)])
    assert FAM.day_maximum(full, event_date=day, settlement_timezone="America/Sao_Paulo") == (pytest.approx(22.5), True)
    morning = _day_curve(day, "America/Sao_Paulo", 0, [15.0] * 12)
    assert FAM.day_maximum(morning, event_date=day, settlement_timezone="America/Sao_Paulo")[1] is False
    # Six-hourly: SBGR's afternoon holds one step (18Z = 15:00), ZUUU's one
    # (06Z = 14:00), a UTC+5:30 station's two (06Z, 12Z).
    six = {f"2026-09-23T{h:02d}:00:00Z": 20.0 + h / 10 for h in (0, 6, 12, 18)}
    assert FAM.day_maximum(six, event_date=day, settlement_timezone="America/Sao_Paulo", step_hours=6)[1] is True
    assert FAM.day_maximum(six, event_date=day, settlement_timezone="Asia/Shanghai", step_hours=6)[1] is True
    no_06 = {k: v for k, v in six.items() if "T06" not in k}
    assert FAM.day_maximum(no_06, event_date=day, settlement_timezone="Asia/Kolkata", step_hours=6)[1] is False


def test_feature_at_reads_the_newest_complete_run_knowable_at_the_clock():
    day = date(2026, 9, 23)
    zone = "America/Sao_Paulo"

    def row(run, knowable, curve):
        return {"ensemble_system_id": "ECMWF_AIFS_SINGLE_POINT", "run_init_utc": run, "knowable_at_utc": knowable,
                "lead_hours_to_local_day_start": 9.0, "hourly_c": curve}

    six = {f"2026-09-23T{h:02d}:00:00Z": 20.0 + h / 10 for h in (0, 6, 12, 18)}
    newer_partial = {"2026-09-23T00:00:00Z": 30.0}
    rows = [
        row("2026-09-22T12:00:00Z", "2026-09-22T17:30:00Z", six),
        row("2026-09-22T18:00:00Z", "2026-09-22T23:40:00Z", dict(six, **{"2026-09-23T18:00:00Z": 25.0})),
        row("2026-09-23T00:00:00Z", "2026-09-23T05:40:00Z", newer_partial),
    ]
    early = FAM.feature_at(None, station_id="SBGR", event_date="2026-09-23", as_of_utc="2026-09-22T20:00:00Z",
                           settlement_timezone=zone, rows=rows)
    assert early["ECMWF_AIFS_SINGLE_POINT"]["run_init_utc"] == "2026-09-22T12:00:00Z"
    late = FAM.feature_at(None, station_id="SBGR", event_date="2026-09-23", as_of_utc="2026-09-23T06:00:00Z",
                          settlement_timezone=zone, rows=rows)
    got = late["ECMWF_AIFS_SINGLE_POINT"]
    assert got["run_init_utc"] == "2026-09-22T18:00:00Z", "the newer run misses the afternoon: not a maximum"
    assert got["day_max_c"] == pytest.approx(25.0)
    assert FAM.feature_at(None, station_id="SBGR", event_date="2026-09-23", as_of_utc="2026-09-23T06:00:00Z",
                          settlement_timezone=zone, rows=rows, systems=["NCEP_NBM_POINT"]) == {}


def test_the_global_roster_leaves_out_a_station_it_cannot_place():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_station_registry(station_id TEXT, timezone_name TEXT)")
    conn.executemany("INSERT INTO calibration_station_registry VALUES(?,?)",
                     [("SBGR", "America/Sao_Paulo"), ("LLBG", "Asia/Jerusalem")])
    roster = FAM.global_roster(conn)
    assert [s[0] for s in roster] == ["SBGR"], "LLBG has a registry row but no coordinates"
    assert roster[0][3] == "America/Sao_Paulo"


def test_the_family_is_not_an_ensemble():
    from bayes_basket_target.official_native_forward_packet import (  # noqa: PLC0415
        REGIONAL_DETERMINISTIC_SYSTEM_IDS,
    )

    for product in ("AIFS", "IFS9KM", "UKMO10"):
        assert L.SYSTEM_IDS[product] not in REGIONAL_DETERMINISTIC_SYSTEM_IDS
    assert {"CMA", "IFS9KM", "UKMO10"} == L.RELAY_PRODUCTS
    assert FAM.FAMILY_SYSTEM_IDS == set(L.SYSTEM_IDS.values())


# ---------------------------------------------------------------------------
# the doorbell makes the capture due once per run
# ---------------------------------------------------------------------------

def _daemon():
    spec = importlib.util.spec_from_file_location(
        "_charter_201_daemon_under_test", ROOT / "scripts" / "weather_basket_yes_daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_doorbell_rings_once_per_run_and_only_for_the_new_members(tmp_path):
    daemon = _daemon()
    state = tmp_path / "probe.json"
    state.write_text(json.dumps({"products": {
        "AIFS": {"newest_wanted_run_utc": "2026-09-23T00:00:00Z", "published": True},
        "UKMO10": {"newest_wanted_run_utc": "2026-09-22T12:00:00Z", "published": False},
        "ICON_EU": {"newest_wanted_run_utc": "2026-09-23T00:00:00Z", "published": True},
    }}))
    due = {"capture_deterministic_forward": 900.0, "capture_dwd_icon_eu_forward": 900.0}
    honoured: dict[str, str] = {}
    assert daemon.honour_deterministic_probe(due, now_mono=100.0, honoured=honoured, path=state) == ["AIFS"]
    assert due == {"capture_deterministic_forward": 100.0, "capture_dwd_icon_eu_forward": 900.0}
    due["capture_deterministic_forward"] = 900.0
    assert daemon.honour_deterministic_probe(due, now_mono=200.0, honoured=honoured, path=state) == []
    assert due["capture_deterministic_forward"] == 900.0


def test_a_capture_pass_skips_the_relays_on_an_exhausted_day(monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import capture_deterministic_forward as script  # noqa: PLC0415

    Q.mark_exhausted()
    planned: list[str] = []
    monkeypatch.setattr(L, "plan_p1", lambda conn, *, product, **k: planned.append(product) or [])
    monkeypatch.setattr(FAM, "product_roster", lambda conn, product: [("SBGR", -23.4, -46.5, "America/Sao_Paulo")])
    monkeypatch.setattr(sys, "argv", ["capture_deterministic_forward.py", "--db", ":memory:"])
    assert script.main() == 0
    assert planned == ["AIFS"], "the relays are not even planned once the day is gone"


def test_a_six_hourly_run_that_holds_the_afternoon_step_is_peak_complete():
    conn = sqlite3.connect(":memory:")
    L.ensure_schema(conn)
    run = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    day = date(2026, 9, 23)
    steps = AIFS.local_day_steps(run_init=run, event_date=day, utc_offset_hours=-3)
    assert steps == [18, 24, 30, 36], "03, 09, 15, 21 local at SBGR"
    L.write_run(conn, product="AIFS", station_id="SBGR", event_date=day,
                settlement_timezone="America/Sao_Paulo", run_init=run,
                hourly_c={h: 20.0 + h / 10 for h in steps}, hours_wanted=4,
                archive_query_at=run + timedelta(hours=6), source_url="gcs://aifs")
    without_afternoon = {h: 20.0 for h in steps if h != 30}
    L.write_run(conn, product="AIFS", station_id="SBGR", event_date=day,
                settlement_timezone="America/Sao_Paulo", run_init=run + timedelta(hours=6),
                hourly_c={h - 6: v for h, v in without_afternoon.items()}, hours_wanted=4,
                archive_query_at=run + timedelta(hours=12), source_url="gcs://aifs")
    flags = dict(conn.execute(f"SELECT run_init_utc, remaining_peak_complete FROM {L.TABLE}").fetchall())
    assert flags == {"2026-09-22T12:00:00Z": 1, "2026-09-22T18:00:00Z": 0}
