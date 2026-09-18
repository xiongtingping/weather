"""The board's regional section and the P1 four-layer chain's own container.

User, 2026-09-19: "前端区域模型板块：没有cma相关的信息显示" and "P1 四层链：前端显式".
The regional section counted the P0 member archive, which the lanes stopped
feeding when charter 95 moved them to P1 -- CMA, which only ever wrote the P1
lane, could not appear at all.  These tests hold the section to the lane P1
actually reads and to every lane that exists.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402
import fit_metar_phase_hazard_artifact as H  # noqa: E402
from bayes_basket_target import regional_point_lane as LANE  # noqa: E402


def test_every_regional_lane_has_a_row_and_a_roster() -> None:
    assert set(C.REGIONAL_LANES) == set(LANE.SYSTEM_IDS.values()), (
        "a lane missing here is a lane the board cannot show -- CMA was"
    )
    assert set(C._capture_rosters()) == set(C.REGIONAL_LANES)
    assert "ZUUU" in C._capture_rosters()["CMA_GRAPES_GLOBAL_POINT"]


def _lane_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "lane.db")
    conn.executescript(LANE.DDL)
    for index, (system, station, run) in enumerate((
        ("CMA_GRAPES_GLOBAL_POINT", "ZUUU", "2026-09-18T06:00:00Z"),
        ("CMA_GRAPES_GLOBAL_POINT", "ZUUU", "2026-09-17T18:00:00Z"),
        ("JMA_MSM_POINT", "RCSS", "2026-09-18T09:00:00Z"),
    )):
        conn.execute(
            f"INSERT INTO {LANE.TABLE} VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"id{index}", system, station, "2026-09-18", "Asia/Shanghai", run,
             int(run[11:13]), -8.0, 30.0, 1, 8, 8, "{}", "T", "E",
             run, run, "u", LANE.SCHEMA_VERSION, f"{index:064d}"),
        )
    conn.commit()
    return conn


def test_the_section_reads_the_lane_p1_reads(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 18, 16, 0, tzinfo=C.timezone.utc))
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({"measured_at_utc": "2026-09-18T16:25:01Z", "systems": {
        "CMA_GRAPES_GLOBAL_POINT": {"verdict": "REFUSE_STRADDLES_ZERO",
                                    "by_station": {"ZUUU": {"delta": -0.25, "n": 66}}}}}))
    monkeypatch.setattr(C, "REGIONAL_ADMISSION_GATE_JSON", gate)
    monkeypatch.setattr(C, "STATUS_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(C, "REGIONAL_SWEEP_JSON", tmp_path / "absent_sweep.json")
    out = C.collect_regional_lanes(_lane_db(tmp_path))
    cma = next(l for l in out["lanes"] if l["system_id"] == "CMA_GRAPES_GLOBAL_POINT")
    assert cma["row_count"] == 2 and cma["station_count"] == 1
    assert cma["last_run_init"] == "2026-09-18T06:00:00Z"
    zuuu = out["stations"]["CMA_GRAPES_GLOBAL_POINT"][0]
    assert zuuu["station_id"] == "ZUUU" and zuuu["runs"] == 2
    assert zuuu["run_age_hours"] == pytest.approx(10.0)
    assert zuuu["usable_now"] is True, "ten hours is inside the twelve-hour limit"
    assert out["admission"]["systems"]["CMA_GRAPES_GLOBAL_POINT"]["verdict"] == (
        "REFUSE_STRADDLES_ZERO"
    )


def test_every_chain_feature_has_a_reader_facing_name() -> None:
    names = set(H.FULL_FEATURES) | set(H.PHASE_FEATURES) | set(H.REGIONAL_FEATURES)
    assert names <= set(C.FOUR_LAYER_FEATURE_WORDS), (
        names - set(C.FOUR_LAYER_FEATURE_WORDS)
    )


def test_the_chain_has_its_own_container() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert '[[fourLayerBoard,"P1 四层链"]]' in source
    assert '"four_layer_chain": four_layer_chain,' in source
