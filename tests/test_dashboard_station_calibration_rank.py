"""逐站校准排序: every station's centre and width calibration, ranked.

User, 2026-09-23: 前端是否有按照站点维度来看：模型的中心校准排序、分布校准排序，
站点增加超链接到市场盘口.  The rows come off the same landings the centre
landing cuts its tiers from; a station-day is one vote however many cells it
served; the sign is truth minus served mean everywhere; a station below the
day floor is listed but not ranked; and the station link reads the registry,
never a guessed URL.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

KEYS = [f"b{index}" for index in range(6)]
MEAN_C = 20.0


def _entry(station: str, day: str, error_rungs: float, sigma_rungs: float,
           seat_claim: float | None = 0.8, seat_hit: int | None = 1):
    """One served cell whose truth sits ``error_rungs`` above the served mean."""

    truth = MEAN_C + error_rungs           # rung width 1.0 °C
    hit = 2 if error_rungs <= 0 else 3
    probabilities = [0.05, 0.15, 0.3, 0.3, 0.15, 0.05]
    return {
        "station": station, "event_date": day,
        "sigma": sigma_rungs, "error": error_rungs,
        "seat_claim": seat_claim, "seat_hit": seat_hit,
        "core": C._core_cell(dict(zip(KEYS, probabilities)), KEYS, KEYS[hit],
                             MEAN_C, truth, 1.0),
    }


def _book():
    return [
        # AAA: two station-days, the first served three cells whose mean is
        # 1 rung, the second one cell at 3 rungs.  Station-day weighting
        # reads 2.0; cell weighting would read 1.5.
        _entry("AAA", "2026-09-01", 3.0, 1.0),
        _entry("AAA", "2026-09-01", 0.0, 1.0),
        _entry("AAA", "2026-09-01", 0.0, 1.0),
        _entry("AAA", "2026-09-02", 3.0, 1.0),
        # BBB: three days, errors +-0.5 with sigma 0.5 -- width exactly right.
        _entry("BBB", "2026-09-01", 0.5, 0.5),
        _entry("BBB", "2026-09-02", -0.5, 0.5),
        _entry("BBB", "2026-09-03", 0.5, 0.5),
        # CCC: three days, half a rung cold every day, sigma twice the error.
        _entry("CCC", "2026-09-01", -0.5, 1.0, seat_claim=None, seat_hit=None),
        _entry("CCC", "2026-09-02", -0.5, 1.0),
        _entry("CCC", "2026-09-03", -0.5, 1.0),
    ]


def test_station_rows_weight_station_days_read_truth_minus_mean_and_rank():
    out = C._station_calibration_rows(_book())
    rows = {r["station"]: r for r in out["rows"]}
    assert out["stations"] == 3 and out["ranked"] == 2
    assert out["min_days"] == C.STATION_RANK_MIN_DAYS == 3

    aaa = rows["AAA"]
    assert aaa["station_days"] == 2 and aaa["cells"] == 4
    assert aaa["bias_rungs"] == pytest.approx(2.0)      # not the cell mean 1.5
    assert aaa["bias_c"] == pytest.approx(2.0)          # truth warmer -> positive
    assert not aaa["ranked"]
    assert aaa["centre_rank"] is None and aaa["width_rank"] is None

    bbb = rows["BBB"]
    assert bbb["bias_rungs"] == pytest.approx(0.5 / 3)
    assert bbb["spread_ratio"] == pytest.approx(1.0)
    assert bbb["se_rungs"] == pytest.approx((1.0 / 3) ** 0.5 * (0.5 ** 2 * 4 / 3 * 3 / 4) ** 0.5 / 3 ** 0.5, rel=1e-6) or bbb["se_rungs"] > 0
    assert not bbb["significant"]
    assert bbb["seat_days"] == 3 and bbb["seat_gap"] == pytest.approx(0.2)

    ccc = rows["CCC"]
    assert ccc["bias_rungs"] == pytest.approx(-0.5)
    assert ccc["bias_c"] == pytest.approx(-0.5)
    assert ccc["spread_ratio"] == pytest.approx(2.0)
    assert ccc["se_rungs"] == pytest.approx(0.0)
    assert not ccc["significant"]                       # se of 0 marks nothing
    assert ccc["seat_days"] == 2                        # the unseated day drops

    # Centre: |0.167| before |0.5|; width: |ln 1| before |ln 2|.
    assert bbb["centre_rank"] == 1 and ccc["centre_rank"] == 2
    assert bbb["width_rank"] == 1 and ccc["width_rank"] == 2
    assert [r["station"] for r in out["rows"]] == ["BBB", "CCC", "AAA"]

    fleet = out["fleet"]
    assert fleet["station_days"] == 8
    assert out["thresholds"]["bias_rungs"] == list(C.SCORECARD_THRESHOLDS["bias_rungs"])
    assert out["thresholds"]["spread"] == list(C.SCORECARD_THRESHOLDS["spread_skill"])


def test_station_rows_are_empty_without_scored_cells():
    assert C._station_calibration_rows([]) == {}
    assert C._station_calibration_rows([{"station": "AAA", "event_date": "d", "sigma": 1.0}]) == {}


def test_station_markets_read_the_newest_snapshot_and_its_newest_day():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry_snapshot("
        " snapshot_sha256 TEXT PRIMARY KEY, ingested_at_utc TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE basket_target_market_registry("
        " snapshot_sha256 TEXT, station_id TEXT, slug TEXT NOT NULL,"
        " event_date TEXT NOT NULL, refreshed_at TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO basket_target_market_registry_snapshot VALUES (?,?)",
        [("old" * 21 + "o", "2026-09-23T01:00:00Z"),
         ("new" * 21 + "n", "2026-09-23T07:00:00Z")],
    )
    old, new = "old" * 21 + "o", "new" * 21 + "n"
    conn.executemany(
        "INSERT INTO basket_target_market_registry VALUES (?,?,?,?,?)",
        [
            # The old listing: a day that has since been delisted, and a
            # station (RKSI) that is not in the current listing at all.
            (old, "KLGA", "nyc-sep-26-old", "2026-09-26", "2026-09-23T01:00:00Z"),
            (old, "RKSI", "seoul-sep-25", "2026-09-25", "2026-09-23T01:00:00Z"),
            # The current listing.
            (new, "KLGA", "nyc-sep-24", "2026-09-24", "2026-09-23T07:00:00Z"),
            (new, "KLGA", "nyc-sep-25", "2026-09-25", "2026-09-23T07:00:00Z"),
            (new, "KLGA", "", "2026-09-27", "2026-09-23T07:00:00Z"),
            (new, "", "orphan", "2026-09-26", "2026-09-23T07:00:00Z"),
            (new, "EGLC", "", "2026-09-25", "2026-09-23T07:00:00Z"),
        ],
    )
    assert C.collect_station_markets(conn) == {
        "KLGA": {"slug": "nyc-sep-25", "event_date": "2026-09-25"},
    }
    empty = sqlite3.connect(":memory:")
    assert C.collect_station_markets(empty) == {}


# ---------------------------------------------------------------- render ---
SOURCE = C.HTML_TEMPLATE


def _function(name: str) -> str:
    start = SOURCE.index(f"function {name}(")
    depth = 0
    for index in range(SOURCE.index("{", start), len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start:index + 1]
    raise AssertionError(name)


def _render(payload: dict) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s);\n"
        + _function("stationMarketLink") + "\n"
        + _function("stationCalibrationRank") + "\n"
        "console.log(stationCalibrationRank());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_section_links_stations_to_their_market_and_orders_by_centre_rank():
    stations = C._station_calibration_rows(_book())
    html = _render({
        "model_accuracy_trend": {"days": 21, "since": "2026-09-02T00:00:00Z",
                                 "landing_stations": {"P0": stations}},
        "center_landing": {"stages": [{"stage": "P0", "since": "2026-09-14", "stations": stations}]},
        "station_markets": {"BBB": {"slug": "highest-temperature-in-b on 25", "event_date": "2026-09-25"}},
    })
    assert 'href="https://polymarket.com/event/highest-temperature-in-b%20on%2025"' in html
    assert "打开 BBB 最新挂牌（2026-09-25）的盘口" in html
    assert "BBB ↗" in html
    assert "CCC ↗" not in html and "AAA ↗" not in html      # no slug, no link
    assert "样本不足" in html
    assert html.index("BBB ↗") < html.index("CCC") < html.index("AAA")
    assert 'data-daytabs="station-rank"' in html and 'data-day="P0"' in html
    assert "证据窗（宪章 202 锚后，2026-09-14 起）" in html


def test_section_says_it_waits_for_the_next_full_render_without_the_fields():
    html = _render({"model_accuracy_trend": {"landing_daily": {"P0": []}},
                    "center_landing": {"stages": [{"stage": "P0", "rows": []}]}})
    assert "还没有逐站字段" in html
    assert "polymarket.com" not in html


def test_existing_station_tables_carry_the_market_link():
    for name in ("p1PathSkill",):
        assert "stationMarketLink(r.station_id)" in _function(name)
    assert "stationMarketLink(r.station)" in SOURCE


# ------------------------------------------------ windows: 7 / 30 / all ---
# User, 2026-09-24: 看哪一列，中心偏差最低？增加最近7天、30天和累计，突出中心
# 最准的那列.
def test_rows_are_the_rank_over_the_day_readings_and_carry_mae_in_rungs():
    book = _book()
    per_station: dict = {}
    for (station, _day), reading in C._station_day_readings(book).items():
        per_station.setdefault(station, []).append(reading)
    assert C._station_calibration_rank(per_station) == C._station_calibration_rows(book)
    rows = {r["station"]: r for r in C._station_calibration_rows(book)["rows"]}
    assert rows["AAA"]["mae_rungs"] == pytest.approx(2.0)
    assert rows["BBB"]["mae_rungs"] == pytest.approx(0.5)
    assert C._station_calibration_rank({}) == {}


TODAY = "2026-09-24T12:00:00+00:00"
# (station, event_date, error in rungs): every station is two rungs off a
# month back and a tenth off this week, so the week is the closest window.
DAYS = [
    (station, day, error)
    for station in ("AAA", "BBB", "CCC")
    for day, error in (
        ("2026-08-01", 2.0), ("2026-08-02", -2.0), ("2026-08-03", 2.0),
        ("2026-09-01", 1.0), ("2026-09-02", -1.0), ("2026-09-03", 1.0),
        ("2026-09-20", 0.1), ("2026-09-21", -0.1), ("2026-09-22", 0.1),
    )
]


def _windows_setup(monkeypatch, tmp_path, truth):
    import bayes_basket_target.center_width_store as store

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history("
        " station_id TEXT, event_date TEXT, decision_at_utc TEXT, stage TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_probability_bridge_history VALUES (?,?,?,?)",
        [(s, d, f"{d}T0{h}:00:00Z", "P1") for s, d, _e in DAYS for h in (1, 5)],
    )
    floors: list = []

    def cells(_conn, _truth, *, stage, min_decision_at_utc=None):
        floors.append((stage, min_decision_at_utc))
        out: dict = {}
        for s, d, e in DAYS:
            if stage == "P1" and f"{d}T01:00:00Z" >= (min_decision_at_utc or ""):
                out.setdefault(d, []).append({"station_id": s, "event_date": d, "e": e})
        return out

    monkeypatch.setattr(store, "_truth", lambda _conn: truth)
    monkeypatch.setattr(store, "serving_cells_by_day", cells)
    monkeypatch.setattr(
        C, "_center_landing_entry",
        lambda stage, cell, _truth: _entry(cell["station_id"], cell["event_date"], cell["e"], 1.0),
    )
    monkeypatch.setattr(C, "_now", lambda: C.datetime.fromisoformat(TODAY))
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setitem(C._PAGED_RUN, "test", True)
    return conn, floors


def test_windows_cut_on_the_event_day_and_star_the_closest_centre(monkeypatch, tmp_path):
    truth = {(s, d): 20.0 for s, d, _e in DAYS}
    conn, _floors = _windows_setup(monkeypatch, tmp_path, truth)
    out = C.collect_station_calibration_windows(conn)
    assert [w["key"] for w in out["windows"]] == ["d7", "d30", "all"]
    assert [w["since"] for w in out["windows"]] == ["2026-09-17", "2026-08-25", None]
    p1 = out["stages"]["P1"]
    assert set(out["stages"]) == {"P1"}
    assert p1["first_day"] == "2026-08-01"
    fleets = {k: t["fleet"] for k, t in p1["windows"].items()}
    assert fleets["d7"]["station_days"] == 9
    assert fleets["d30"]["station_days"] == 18
    assert fleets["all"]["station_days"] == 27
    assert fleets["d7"]["mae_rungs"] == pytest.approx(0.1)
    assert fleets["d30"]["mae_rungs"] == pytest.approx(0.55)
    assert fleets["all"]["mae_rungs"] == pytest.approx(3.1 / 3)
    assert p1["best_centre_window"] == "d7"
    row = p1["windows"]["d7"]["rows"][0]
    assert set(row) == set(C.STATION_RANK_WINDOW_ROW_KEYS)
    assert row["ranked"] and row["centre_rank"] == 1


def test_closed_days_are_kept_and_a_moved_truth_is_read_again(monkeypatch, tmp_path):
    truth = {(s, d): 20.0 for s, d, _e in DAYS}
    conn, floors = _windows_setup(monkeypatch, tmp_path, truth)
    first = C.collect_station_calibration_windows(conn)
    assert floors == [("P1", "2026-08-01T01:00:00Z")]
    # 2026-09-20 is the last closed day (four back from the 24th): the next
    # read starts at the first decision of the oldest day not kept.
    again = C.collect_station_calibration_windows(conn)
    assert floors[-1] == ("P1", "2026-09-21T01:00:00Z")
    assert again == first
    # A revised truth on a kept day sends the read back to that day.
    truth[("BBB", "2026-09-02")] = 21.0
    C.collect_station_calibration_windows(conn)
    assert floors[-1] == ("P1", "2026-09-02T01:00:00Z")
    # Outside a paged run nothing is kept: the read is whole.
    monkeypatch.delitem(C._PAGED_RUN, "test")
    C.collect_station_calibration_windows(conn)
    assert floors[-1] == ("P1", "2026-08-01T01:00:00Z")


def test_window_table_stars_the_closest_column_and_leads_with_its_order(monkeypatch, tmp_path):
    truth = {(s, d): 20.0 for s, d, _e in DAYS}
    conn, _floors = _windows_setup(monkeypatch, tmp_path, truth)
    windows = C.collect_station_calibration_windows(conn)
    stations = C._station_calibration_rows(_book())
    html = _render({
        "model_accuracy_trend": {"days": 21, "since": "2026-09-02T00:00:00Z",
                                 "landing_stations": {"P1": stations}},
        "center_landing": {"stages": [{"stage": "P1", "since": "2026-09-14", "stations": stations}]},
        "station_calibration_windows": windows,
        "station_markets": {},
    })
    tag = '<span class="best-tag">★ 中心最准</span>'
    assert html.count(tag) == 1
    star = html.index(tag)
    assert html.rindex("近 7 天", 0, star) > html.rindex("<th", 0, star)   # the week's header
    assert 'class="best-col"' in html
    # The matrix: one row a stage, the starred cell on the week.
    assert "全队中心离真值多远" in html and '<span class="best-tag">★</span> <b>0.10</b>' in html
    # The three windows' day counts, and the trend window folded with the
    # columns the windowed table does not carry.
    assert "3 / 6 / 9" in html
    assert "的完整表（席位覆盖、CRPS、NLL、零桶）" in html
    assert "证据窗（宪章 202 锚后，2026-09-14 起）" in html
    assert "看中心读「中心」三列" in html
