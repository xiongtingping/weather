"""The tropical rules card (user 2026-10-18 「把热带站的规律在热带站显示评判出来」
「热带站增加一个过去15天的最高温曲线」): the day read against
docs/research/tropical_tmax_regimes_20261018 at WSSS/WMKK only."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bayes_basket_target import station_day_board as board

ROOT = Path(__file__).resolve().parents[1]
SG = ZoneInfo("Asia/Singapore")


def _truth(rows: dict[str, float]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_station_daily_high_truth (station_id TEXT, local_date TEXT,"
                 " official_tmax_c REAL, source_lane TEXT, qc_status TEXT, revision_no INTEGER)")
    conn.executemany("INSERT INTO calibration_station_daily_high_truth VALUES ('WSSS',?,?,'AWC','ACCEPTED',0)",
                     list(rows.items()))
    return conn


TRUTH = {f"2026-10-{d:02d}": t for d, t in zip(range(3, 18), [32, 33, 32, 33, 33, 32, 33, 33, 33, 33, 33, 32, 31, 33, 32])}


def _metar(local: str, raw_wx: str = "") -> dict:
    return {"local": local, "raw": f"WSSS 180000Z 18005KT 9999 {raw_wx} FEW018 31/25 Q1008".replace("  ", " "),
            "temp_c": 31.0}


@pytest.fixture(autouse=True)
def _no_archive(monkeypatch, tmp_path):
    monkeypatch.setattr(board, "ARCHIVE_DB", tmp_path / "missing.db")


def test_other_stations_and_days_not_begun_get_nothing() -> None:
    assert board._tropical_rules(_truth(TRUTH), "ZUUU", "2026-10-18", SG, []) is None
    # 扩站: the four pre-registered conditions failed at these -- no card
    for station in ("ZGSZ", "OEJN", "MPMG", "MMMX"):
        assert board._tropical_rules(_truth(TRUTH), station, "2026-10-18", SG, []) is None
    assert board._tropical_rules(_truth(TRUTH), "WSSS", "2026-10-19", SG, [],
                                 now=datetime(2026, 10, 18, 4, tzinfo=timezone.utc)) is None


def test_rain_before_ten_reads_cooling_with_the_study_rates() -> None:
    out = board._tropical_rules(_truth(TRUTH), "WSSS", "2026-10-18", SG,
                                [_metar("07:30", "-TSRA")], now=datetime(2026, 10, 18, 4, tzinfo=timezone.utc))
    assert out["verdict"] == "偏降温" and (out["cool_pct"], out["warm_pct"]) == (62, 9)
    assert out["base_c"] == 33 and out["cool_at_or_below_c"] == 32 and out["warm_at_or_above_c"] == 34
    assert out["prev_c"] == 32 and out["prev_anom_c"] == -1 and out["rain_before_10"] is True
    # fifteen days of maxima for the curve, the base from the last ten
    assert [h["date"] for h in out["history"]][0] == "2026-10-03" and len(out["history"]) == 15


def test_night_rain_before_six_is_not_rain_before_ten() -> None:
    """User 2026-10-18 「只统计白天开始的降雨，夜里的残雨不算"10 点前下雨"」:
    WMKK's 00:00/00:30 -RA that stopped by 01:00 read 偏降温 52%."""

    now = datetime(2026, 10, 18, 4, tzinfo=timezone.utc)
    out = board._tropical_rules(_truth(TRUTH), "WSSS", "2026-10-18", SG,
                                [_metar("00:00", "-RA"), _metar("00:30", "-RA"), _metar("01:00")], now=now)
    assert out["rain_before_10"] is False and out["first_wet_local"] is None
    assert out["night_wet_local"] == "00:00" and "下雨" not in out["basis"]
    assert out["basis"].startswith("昨天比基准")
    # the night shower does not hide the day's own: rain from 06:00 still counts
    out = board._tropical_rules(_truth(TRUTH), "WSSS", "2026-10-18", SG,
                                [_metar("00:00", "-RA"), _metar("05:30"), _metar("06:00", "-SHRA")], now=now)
    assert out["rain_before_10"] is True and out["first_wet_local"] == "06:00"
    assert out["night_wet_local"] == "00:00" and (out["cool_pct"], out["warm_pct"]) == (62, 9)


def test_without_rain_or_morning_reports_it_falls_back_to_yesterday() -> None:
    out = board._tropical_rules(_truth(TRUTH), "WSSS", "2026-10-18", SG, [_metar("08:00")],
                                now=datetime(2026, 10, 18, 0, tzinfo=timezone.utc))
    assert out["basis"].startswith("昨天比基准 -1") and (out["cool_pct"], out["warm_pct"]) == (43, 18)
    assert out["verdict"] == "偏降温" and out["morning_due"] is True
    # VCSH / recent-rain groups are not rain at the field
    assert board._wet_report("WSSS 180000Z 18005KT 9999 VCSH FEW018 31/25 Q1008") is False
    assert board._wet_report("WSSS 180000Z 18005KT 9999 RERA FEW018 31/25 Q1008") is False


def test_the_card_draws_the_verdict_and_the_fifteen_day_curve() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the card")
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    src = C.HTML_TEMPLATE

    def fn(name):
        s = src.index(f"function {name}(")
        depth = 0
        for i in range(src.index("{", s), len(src)):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            if depth == 0:
                return src[s:i + 1]
    T = board._tropical_rules(_truth(TRUTH), "WSSS", "2026-10-18", SG, [_metar("07:30", "-TSRA")],
                              now=datetime(2026, 10, 18, 4, tzinfo=timezone.utc))
    script = ("const SD_COL={P0:'blue'};const sdEsc=s=>String(s==null?'':s);"
              "const sdN=(v,d)=>Number(v).toFixed(d);const sdSg=(v,d)=>(v>0?'+':v<0?'−':'')+Math.abs(v).toFixed(d);\n"
              + fn("sdTropicalCurve") + fn("sdTropicalRules")
              + f"\nconsole.log(sdTropicalRules({{tropical: {json.dumps(T, ensure_ascii=False)}}}));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    html = out.stdout
    assert "热带规律 · WSSS" in html and "偏降温" in html and "降温 62%" in html and "突破 9%" in html
    assert "07:30" in html and "<svg" in html and html.count("<circle") == 16   # 15 days + today
    assert "10-03" in html and "当日已报最高 31°C" in html and ">当日</text>" in html
    assert sdTropical_absent(node, fn)


def sdTropical_absent(node, fn) -> bool:
    script = ("const SD_COL={P0:'blue'};const sdEsc=s=>String(s==null?'':s);\n" + fn("sdTropicalCurve") + fn("sdTropicalRules")
              + "\nconsole.log(JSON.stringify(sdTropicalRules({tropical: null})));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    return out.stdout.strip() == '""'


def test_extended_stations_read_their_own_tables() -> None:
    """扩站 (user 2026-10-18): RPLL and ZGGG passed all four conditions and
    read extend.py's numbers; they were never checked against the market."""

    conn = _truth(TRUTH)
    conn.execute("UPDATE calibration_station_daily_high_truth SET station_id='RPLL'")
    out = board._tropical_rules(conn, "RPLL", "2026-10-18", SG, [_metar("07:30", "-TSRA")],
                                now=datetime(2026, 10, 18, 4, tzinfo=timezone.utc))
    assert (out["cool_pct"], out["warm_pct"]) == (59, 15) and out["market_checked"] is False
    assert out["month"] == (40, 33)
    rates = json.loads((ROOT / "docs/research/tropical_tmax_regimes_20261018/extend_rates.json").read_text())
    for station in board.TROPICAL_STATIONS:
        assert rates[station]["holds"] is True
        if station not in board.TROPICAL_MARKET_CHECKED:
            assert {k: tuple(v) for k, v in rates[station]["rates"].items() if k in board.TROPICAL_RATES[station]} \
                == board.TROPICAL_RATES[station]
            assert tuple(tuple(m) for m in rates[station]["month"]) == board.TROPICAL_MONTH_RATES[station]
    assert not any(v["holds"] for k, v in rates.items() if k not in board.TROPICAL_STATIONS)


def test_a_day_without_an_envelope_still_carries_reports_taf_and_rain(monkeypatch) -> None:
    """User 2026-10-18 「这一天还没有任何已提交的概率信封。不要影响其他内容显示，
    比如taf、metar、实况等」: only the probability parts are missing."""

    monkeypatch.setattr(board, "_envelope_head", lambda *a: None)
    monkeypatch.setattr(board, "_metar", lambda *a: [_metar("07:30", "-TSRA")])
    for name in ("_taf", "_precip", "_deterministic"):
        monkeypatch.setattr(board, name, lambda *a, _n=name: {"stub": _n})
    for name in ("_precip_sat", "_precip_5min", "_hourly_vs_forecast", "_direction", "_d0", "_member_bayes"):
        monkeypatch.setattr(board, name, lambda *a, _n=name: {"stub": _n})
    monkeypatch.setattr(board, "_tropical_rules", lambda *a: {"stub": "tropical"})
    out = board._day(_truth(TRUTH), "WSSS", "2026-10-18", "today", SG)
    assert out["available"] is False and out["stages"] == {} and out["market"]["available"] is False
    assert out["metar"] and out["running_high_c"] == 31.0
    for key in ("taf", "precip", "precip_sat", "hourly_sky", "deterministic", "d0", "tropical"):
        assert out[key], key


def test_a_timeline_entry_names_what_moved_it() -> None:
    """User 2026-10-18 「曲线上增加曲线变化原因，比如模型run更新等」: read off the
    lineages of the decision and the stage's one before."""

    runs = lambda **kw: json.dumps({f"{k}": f"2026-10-18T{v}:00:00Z" for k, v in kw.items()})  # noqa: E731
    before = (runs(ECMWF_AIFS_ENS_GLOBAL="18", NOAA_GEFS_05="00"), "taf-1", "21", "18", "root-1")
    assert board._timeline_cause("P0", before, None) == ["当日首次决策"]
    now = (runs(ECMWF_AIFS_ENS_GLOBAL="00", NOAA_GEFS_05="00", DWD_ICON_GLOBAL_EPS="00"), "taf-1", "21", "18", "root-1")
    assert board._timeline_cause("P0", now, before) == ["ICON 00Z 入座", "AIFS 18Z→00Z"]
    assert board._timeline_cause("P0", before, before) == ["同一批 run 重算"]
    assert board._timeline_cause("P1", before[:3] + ("19", "root-1"), before) == ["已报最高 18→19°C"]
    assert board._timeline_cause("P1", before[:4] + ("root-2",), before) == ["前序阶段更新"]
    assert board._timeline_cause("P1", before, before) == ["新报文"]


def _registry(rows: list[tuple[str, float | None, float | None, str]]) -> sqlite3.Connection:
    """Rows (outcome, lower, upper, snapshot) in the registry's own shape:
    every snapshot keeps its rows, ingested in the order given."""

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_target_market_registry_snapshot (snapshot_sha256 TEXT, ingested_at_utc TEXT)")
    conn.execute("CREATE TABLE basket_target_market_registry (snapshot_sha256 TEXT, station_id TEXT, event_date TEXT,"
                 " settlement_unit TEXT, outcome TEXT, registry_json TEXT)")
    snaps = list(dict.fromkeys(snap for *_rest, snap in rows))
    conn.executemany("INSERT INTO basket_target_market_registry_snapshot VALUES (?,?)",
                     [(snap, f"2026-10-18T0{i}:00:00Z") for i, snap in enumerate(snaps)])
    conn.executemany("INSERT INTO basket_target_market_registry VALUES (?,'WSSS','2026-10-18','C',?,?)",
                     [(snap, o, json.dumps({"lower": lo, "upper": hi})) for o, lo, hi, snap in rows])
    return conn


def test_a_day_without_an_envelope_gets_its_ladder_from_the_registry() -> None:
    """User 2026-10-18 「新加坡没有下单器」: the registry keeps every refresh,
    so the same ladder appears many times; the newest row per bucket counts."""

    ladder = [("25°C or below", None, 25.0), ("26°C", 26.0, 26.0), ("27°C or higher", 27.0, None)]
    conn = _registry([(o, lo, hi, at) for at in ("2026-10-17T00:00Z", "2026-10-18T00:00Z") for o, lo, hi in ladder])
    axis = board._registry_axis(conn, "WSSS", "2026-10-18")
    assert [(a["bucket"], a["lo_c"], a["hi_c"], a["ordinal"]) for a in axis] == [
        ("25°C or below", None, 25.5, 0), ("26°C", 25.5, 26.5, 1), ("27°C or higher", 26.5, None, 2)]
    # a ladder with a hole is no ladder (fail closed), and so is no table at all
    assert board._registry_axis(_registry([("25°C or below", None, 25.0, "x"), ("27°C or higher", 27.0, None, "x")]),
                                "WSSS", "2026-10-18") is None
    assert board._registry_axis(sqlite3.connect(":memory:"), "WSSS", "2026-10-18") is None
