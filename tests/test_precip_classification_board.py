"""降水分类评分 on the board (user, 2026-09-23: 「增加：降水分类评分」).

Six scores off one 2×2 contingency table, every forecast-observation pair
classed wet or dry at 1.0 mm.  The forecast is Open-Meteo's daily
precipitation sum off the physics version table, resolved as-of each lead's
cut; the observation is the official METAR/SPECI of the station's local day.
An ASOS station's hourly ``Pxxxx`` remark gives the day in millimetres; every
other station only says whether it was raining, and the two readings are kept
on separate cards.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import calibration_dashboard as C  # noqa: E402
import capture_station_forecast_physics as CAP  # noqa: E402
from weather_basket_yes_schema import apply_weather_basket_yes_schema  # noqa: E402


def test_the_six_scores_are_the_textbook_formulas() -> None:
    s = C.precip_classification_scores(30, 10, 20, 140)
    assert s["n"] == 200
    assert s["pod"] == pytest.approx(0.75)
    assert s["far"] == pytest.approx(0.4)
    assert s["csi"] == pytest.approx(0.5)
    # a_r = (a+b)(a+c)/N = 40*50/200 = 10
    assert s["ets"] == pytest.approx((30 - 10) / (60 - 10))
    assert s["hss"] == pytest.approx(2 * (30 * 140 - 10 * 20) / (40 * 150 + 50 * 160))
    assert s["bias"] == pytest.approx(1.25)


def test_a_zero_denominator_is_none_not_a_number() -> None:
    s = C.precip_classification_scores(0, 0, 0, 5)
    assert s["pod"] is None and s["far"] is None and s["csi"] is None
    assert s["bias"] is None and s["ets"] is None
    empty = C.precip_classification_scores(0, 0, 0, 0)
    assert all(empty[k] is None for k in ("pod", "far", "csi", "ets", "hss", "bias"))
    one_hit = C.precip_classification_scores(1, 0, 0, 0)
    assert one_hit["pod"] == 1.0 and one_hit["ets"] is None and one_hit["hss"] is None


@pytest.mark.parametrize(
    ("raw", "present", "hourly_mm", "asos"),
    [
        # A US hourly METAR: the past hour's amount lives in the remarks; the
        # RAE1858 there is history, not present weather.
        ("METAR KLAX 081953Z 28013KT 10SM FEW045 SCT090 29/19 A2992 RMK AO2 LTG DSNT NE"
         " RAE1858 SLP129 P0000 T02940194 $", False, 0.0, True),
        ("METAR KORD 210451Z 07012KT 10SM FEW007 BKN015 OVC031 17/14 A3010 RMK AO2"
         " DZE35RAB35E37 SLP190 P0001 T01720144 $", False, 0.254, True),
        ("METAR KATL 191253Z 09008KT 2SM +TSRA BR BKN008 OVC020 22/21 A3005 RMK AO2"
         " SLP172 P0120 T02220211", True, 120 * 0.254, True),
        # The augmented form military fields file.
        ("METAR KBKF 262258Z 31022KT 10SM FEW140 34/08 A3010 RMK AO2A PK WND 32029/38"
         " RAB07E38 SLP089 P0000 T03440082", False, 0.0, True),
        # SLP133 carries a P followed by digits inside a word: not a P group.
        ("METAR KLAX 082153Z 26016KT 10SM FEW050 27/19 A2993 RMK AO2 SLP133 SHRA N AND NW"
         " T02720194 $", False, None, True),
        # Non-US reports: present weather only, no amount, no AO remark.
        ("METAR EGLC 221920Z AUTO 15007KT 110V200 9999 -RA NCD 21/14 Q1027", True, None, False),
        ("METAR ZUUU 222300Z VRB01MPS 3500 -SHRA BR FEW006 SCT014 21/20 Q1016 NOSIG", True, None, False),
        ("METAR XXXX 010000Z 00000KT 9999 FZDZ SCT020 M02/M04 Q1015", True, None, False),
        # Vicinity showers, recent rain and a TEMPO forecast are not rain at
        # the station at observation time; a thunderstorm without
        # precipitation and blowing snow are not falling precipitation.
        ("METAR XXXX 010000Z 00000KT 9999 VCSH SCT020 20/10 Q1015 TEMPO SHRA", False, None, False),
        ("METAR XXXX 010000Z 00000KT 9999 RERA SCT020 20/10 Q1015", False, None, False),
        ("METAR XXXX 010000Z 00000KT 9999 TS SCT020 20/10 Q1015", False, None, False),
        ("METAR XXXX 010000Z 30025KT 2000 BLSN OVC010 M05/M08 Q1015", False, None, False),
        ("METAR XXXX 010000Z 00000KT 9999 SCT020 20/10 Q1015 BECMG -RA", False, None, False),
        ("", False, None, False),
    ],
)
def test_one_report_is_read_for_presence_amount_and_station_kind(
    raw: str, present: bool, hourly_mm: float | None, asos: bool
) -> None:
    reading = C.metar_precipitation_reading(raw)
    assert reading["present"] is present
    if hourly_mm is None:
        assert reading["hourly_mm"] is None
    else:
        assert reading["hourly_mm"] == pytest.approx(hourly_mm)
    assert reading["asos"] is asos


def _archive(root: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
    """Production DDL; rows are (observation_id, station, report_type, valid_at, raw)."""

    db = sqlite3.connect(root / "data" / "archive.db")
    apply_weather_basket_yes_schema(db)
    for oid, station, report_type, valid_at, raw in rows:
        db.execute(
            "INSERT INTO reported_trajectory_observations(observation_id,source_lane,"
            "station_id,report_type,valid_at,reported_temperature_c,raw_sha256,raw_text,"
            "first_seen_at,parsed_at,canonical_committed_at,fetch_started_at,"
            "fetch_completed_at,source_poll_id,correction_suspected)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, "awc", station, report_type, valid_at, 20, oid.ljust(64, "0"), raw,
             valid_at, valid_at, valid_at, valid_at, valid_at, f"poll-{oid}", 0),
        )
    db.commit()
    db.close()


def _ssot(rows: list[tuple[str, str, float, str, str, str]]) -> sqlite3.Connection:
    """The physics version table from the capture script's own DDL; rows are
    (station, local_date, precipitation_sum, capture_mode, captured_at, zone)."""

    conn = sqlite3.connect(":memory:")
    conn.executescript(CAP.VERSION_SCHEMA)
    for i, (station, day, mm, mode, captured, zone) in enumerate(rows):
        conn.execute(
            f"INSERT INTO {CAP.VERSION_TABLE} ({CAP.COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (station, day, CAP.SOURCE_ID, 10.0, 5.0, 50.0, mm, 0.0, 0.0, zone, mode,
             captured, 1, f"{i:064x}"),
        )
    conn.commit()
    return conn


def _hourly(station: str, first_utc: str, count: int, minute: str, remark: str,
            wx: dict[int, str] | None = None, pgroup: dict[int, int] | None = None,
            ) -> list[tuple[str, str, str, str, str]]:
    """``count`` hourly METARs from ``first_utc`` (YYYY-MM-DDTHH), one per hour.
    ``wx`` puts a present-weather token in hour k's body; ``pgroup`` puts a
    Pxxxx group (hundredths of an inch) in hour k's remarks."""

    from datetime import datetime, timedelta  # noqa: PLC0415

    start = datetime.strptime(first_utc, "%Y-%m-%dT%H")
    out = []
    for k in range(count):
        at = start + timedelta(hours=k)
        stamp = at.strftime(f"%Y-%m-%dT%H:{minute}:00.000Z")
        weather = (wx or {}).get(k)
        body = (f"METAR {station} {at.strftime('%d%H')}{minute}Z 00000KT 9999 "
                + (weather + " " if weather else "") + "SCT020 20/10 Q1015")
        amount = (pgroup or {}).get(k)
        raw = body + remark + (f" P{amount:04d}" if amount is not None else "")
        out.append((f"{station}-{first_utc}-{k}", station, "METAR", stamp, raw))
    return out


def test_pairs_are_classed_per_lead_and_the_two_readings_stay_apart(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(C, "ROOT", tmp_path)
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 5, 0, 0, tzinfo=C.timezone.utc))
    (tmp_path / "data").mkdir()
    ny, sh = "America/New_York", "Asia/Shanghai"
    # KAAA, New York: local 2026-09-01 runs 04:00Z 09-01 -> 04:00Z 09-02.  Two of
    # the twenty-four hourly METARs carry a P group (20 + 30 hundredths of an
    # inch = 12.7 mm); the 14:53Z one and a SPECI carry -RA.
    rows = _hourly("KAAA", "2026-09-01T04", 24, "53", " RMK AO2 SLP150",
                   wx={10: "-RA"}, pgroup={10: 20, 14: 30})
    rows.append(("KAAA-speci", "KAAA", "SPECI", "2026-09-01T14:20:00.000Z",
                 "SPECI KAAA 011420Z 00000KT 9999 -RA SCT020 20/10 Q1015 RMK AO2"))
    # KBBB, New York: only ten hourly METARs on 09-01 -- coverage too thin.
    rows += _hourly("KBBB", "2026-09-01T04", 10, "53", " RMK AO2 SLP150", pgroup={3: 100})
    # ZZZZ, Shanghai: local 2026-09-01 runs 16:00Z 08-31 -> 16:00Z 09-01; the
    # 17:00Z 08-31 report (01:00 local) carries -SHRA, so the day is wet by
    # presence and the pairing has to convert to the local day to see it.
    rows += _hourly("ZZZZ", "2026-08-31T16", 24, "00", "", wx={1: "-SHRA"})
    # KAAA 09-04 is still open at the frozen now (its day ends 04:00Z 09-05).
    rows += _hourly("KAAA", "2026-09-04T04", 24, "53", " RMK AO2 SLP150")
    _archive(tmp_path, rows)

    conn = _ssot([
        # KAAA 09-01: three forward captures and one historical row.
        ("KAAA", "2026-09-01", 0.0, "forward", "2026-08-29T12:00:00Z", ny),    # d2, d3
        ("KAAA", "2026-09-01", 3.0, "forward", "2026-08-31T20:00:00Z", ny),    # d1
        ("KAAA", "2026-09-01", 0.2, "forward", "2026-09-01T18:00:00.123456Z", ny),  # same_day
        ("KAAA", "2026-09-01", 5.0, "historical", "2026-09-10T00:00:00Z", ny),  # after
        ("KAAA", "2026-09-04", 0.0, "forward", "2026-09-04T18:00:00Z", ny),
        ("KBBB", "2026-09-01", 1.5, "forward", "2026-09-01T18:00:00Z", ny),
        ("ZZZZ", "2026-09-01", 0.0, "forward", "2026-08-31T10:00:00Z", sh),    # d1
        ("ZZZZ", "2026-09-01", 2.0, "forward", "2026-09-01T06:00:00Z", sh),    # same_day
    ])
    out = C.collect_precip_classification(conn)
    assert out["available"] is True
    assert out["asos_stations"] == ["KAAA", "KBBB"]
    assert out["open_days"] == 1
    mm = next(d for d in out["definitions"] if d["key"] == "mm")["leads"]
    wx = next(d for d in out["definitions"] if d["key"] == "wx")["leads"]

    # Millimetres, same day: KAAA observed 12.7 mm, forecast 0.2 -> a miss;
    # ZZZZ is not an ASOS station and KBBB has ten hours of reports.
    assert (mm["same_day"]["a"], mm["same_day"]["b"], mm["same_day"]["c"], mm["same_day"]["d"]) == (0, 1, 0, 0)
    assert mm["same_day"]["excluded"] == {"not_asos": 1, "coverage": 1}
    assert mm["same_day"]["pod"] == 0.0 and mm["same_day"]["far"] is None
    recent = mm["same_day"]["recent"]
    assert len(recent) == 1 and recent[0]["observed_mm"] == pytest.approx(12.7)
    assert recent[0]["forecast_mm"] == pytest.approx(0.2) and recent[0]["hours"] == 24
    # The day-ahead capture said 3.0 mm: a hit.  Two and three days ahead the
    # only capture said 0.0: misses.  The historical row said 5.0: a hit.
    assert (mm["d1"]["a"], mm["d1"]["b"]) == (1, 0) and mm["d1"]["bias"] == 1.0
    assert (mm["d2"]["a"], mm["d2"]["b"]) == (0, 1)
    assert (mm["d3"]["a"], mm["d3"]["b"]) == (0, 1)
    assert (mm["after"]["a"], mm["after"]["b"]) == (1, 0)
    assert mm["d1"]["stations"][0]["station_id"] == "KAAA"
    assert mm["d1"]["stations"][0]["observed_wet_days"] == 1

    # Presence, same day: KAAA wet (-RA) against 0.2 -> miss; ZZZZ wet (01:00
    # local -SHRA) against 2.0 -> hit.  Day-ahead the two swap: KAAA hit at
    # 3.0, ZZZZ miss at 0.0.
    assert (wx["same_day"]["a"], wx["same_day"]["b"], wx["same_day"]["c"], wx["same_day"]["d"]) == (1, 1, 0, 0)
    assert wx["same_day"]["excluded"] == {"coverage": 1}
    assert wx["same_day"]["pod"] == pytest.approx(0.5) and wx["same_day"]["far"] == 0.0
    assert (wx["d1"]["a"], wx["d1"]["b"]) == (1, 1)
    # ZZZZ has no capture two days out: unpaired there, not dry.  KBBB has
    # none either, and a day with no forecast is counted there before its
    # coverage is looked at.
    assert wx["d2"]["n"] == 1 and wx["d2"]["excluded"] == {"no_forecast": 2}
    assert {r["station_id"] for r in wx["same_day"]["stations"]} == {"KAAA", "ZZZZ"}
    zzzz = next(p for p in wx["same_day"]["recent"] if p["station_id"] == "ZZZZ")
    assert zzzz["observed_present"] is True and zzzz["observed_mm"] is None


def test_no_version_table_is_reported_not_raised() -> None:
    conn = sqlite3.connect(":memory:")
    out = C.collect_precip_classification(conn)
    assert out == {"available": False, "reason": "no_forecast_physics_versions"}
