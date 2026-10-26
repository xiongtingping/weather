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
    conn.execute(
        f"CREATE VIEW calibration_station_registry AS SELECT DISTINCT station_id"
        f" FROM {LANE.TABLE}"
    )
    conn.commit()
    return conn


def test_the_section_reads_the_lane_p1_reads(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 18, 16, 0, tzinfo=C.timezone.utc))
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
    assert "admission" not in out, "the path prior's gate retired with it (charter 150)"


def test_every_likelihood_feature_has_a_reader_facing_name() -> None:
    from bayes_basket_target import p1_observation_likelihood as OL

    names = set(OL.FULL_FEATURES) | set(OL.PHASE_FEATURES)
    assert names <= set(C.P1_LIKELIHOOD_FEATURE_WORDS), (
        names - set(C.P1_LIKELIHOOD_FEATURE_WORDS)
    )


def test_the_observation_update_has_its_own_container() -> None:
    """Charter 150: the four-layer container became the joint likelihood's
    (P0-M's TAF likelihood board left with charter 283).  Charter 161
    (2026-09-20) replaced g**alpha with the update fitted on the final P1,
    so the section is named for the slot rather than for one of its rules."""

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert '[p1JointBoard,"P1 观测更新"]' in source
    assert '[p1PathSkill,"P1 温度路径命中率"]' in source
    assert "p0mTafBoard" not in source and "p0m_taf_likelihood" not in source
    assert '"p1_joint_likelihood": p1_joint_likelihood,' in source
    assert "fourLayerBoard" not in source


def test_the_path_skill_reads_heads_and_leads(tmp_path) -> None:
    """Lock, peak and lead on a hand-built station-day.

    Heads at 02:00Z and 03:30Z (10:00 and 11:30 in Shanghai): the first names
    30C and is wrong, the second names 31C and stays; the thermometer reaches
    31C at 06:00Z.  A replayed step in the same second as the second head,
    stamped a microsecond earlier, must not vote.
    """

    conn = sqlite3.connect(tmp_path / "p1.db")
    conn.executescript(
        "CREATE TABLE calibration_station_daily_high_truth(station_id, local_date,"
        " official_tmax_c, revision_no, source_lane, qc_status);"
        "CREATE TABLE basket_probability_envelope(station_id, event_date,"
        " decision_at_utc, probabilities_json, bucket_ordinals_json, lineage_json,"
        " settlement_timezone, stage, state);"
    )
    conn.execute("INSERT INTO calibration_station_daily_high_truth VALUES"
                 " ('ZUUU','2026-09-18',31.2,1,'AWC','ACCEPTED')")
    intervals = json.dumps({"30": [30.0, 31.0], "31": [31.0, 32.0], "32": [32.0, None]})
    ords = json.dumps({"30": 0, "31": 1, "32": 2})
    for at, probs, high in (
        ("2026-09-18T02:00:00.000000Z", {"30": .6, "31": .3, "32": .1}, 29.0),
        ("2026-09-18T03:30:00.000000Z", {"30": .8, "31": .1, "32": .1}, 29.5),
        ("2026-09-18T03:30:00.000001Z", {"30": .2, "31": .7, "32": .1}, 30.0),
        ("2026-09-18T06:00:00.000000Z", {"30": .0, "31": .9, "32": .1}, 31.0),
    ):
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?,?)",
            ("ZUUU", "2026-09-18", at, json.dumps(probs), ords,
             json.dumps({"bucket_intervals_c_json": intervals,
                         "metar_trajectory_running_high_c": high}),
             "Asia/Shanghai", "P1", "COMMITTED"))
    out = C.collect_p1_path_skill(conn)
    day = out["stations"][0]
    assert day["hit_10"] == 0.0 and day["hit_12"] == 1.0, (
        "10:00 reads the 02:00Z head (30C); 12:00 the 03:30Z head's last step (31C)"
    )
    assert day["median_lead_hours"] == pytest.approx(2.5), "locked 03:30Z, reached 06:00Z"
    assert day["predicted_share"] == 1.0


def _direct_update_block(state: str = "SERVE") -> dict:
    """A well-formed charter 161 block, laid out by production's own x_layout
    so the fixture cannot drift from the thing it stands in for."""

    from bayes_basket_target import p1_direct_update as DU

    width = len(DU.x_layout())
    return {
        "schema": DU.SCHEMA, "rule": DU.RULE, "state": state, "veto": "",
        "lambda": 0.5, "rows": 100,
        "x_layout": DU.x_layout(),
        "theta_rise": [0.1] * width, "theta_slope": [-0.2] * width,
        "theta_slope_sq": -0.05, "theta_open_top": -0.4,
        "mean": [0.0] * len(DU.STATE_COLUMNS),
        "scale": [1.0] * len(DU.STATE_COLUMNS),
        "fallbacks": {},
        "trained_from": "2026-08-06", "trained_through": "2026-09-19",
        "walk_forward": {
            "days": 2, "margin": 0.01, "training_cut": "strict_as_of_truth_commit_v1",
            "gain_vs_alpha_nat": 0.0127, "gain_vs_alpha_ci": [0.0009, 0.0232],
            "gain_vs_root_nat": 0.337,
            "direct_nat_by_day": {"2026-09-18": -0.80, "2026-09-19": -0.77},
            "alpha_nat_by_day": {"2026-09-18": -0.81, "2026-09-19": -0.79},
            "lambda_by_day": {"2026-09-18": 0.3, "2026-09-19": 0.3},
        },
    }


def test_the_board_asks_production_whether_charter_161_serves() -> None:
    """The page must not have its own opinion about which rule is live.

    Charter 161's block and charter 150's g**alpha ride in one artifact and
    a head under either says APPLIED, so a board that read the state string
    itself could say "161 serving" about a block production refuses.  The
    brief calls ``p1_direct_update.serving_block`` -- the same call
    ``materialize_p1`` makes.
    """

    serving = C._p1_direct_update_brief({"direct_update": _direct_update_block()})
    assert serving["present"] is True and serving["serving"] is True
    assert serving["walk_forward"]["gain_vs_alpha_nat"] == 0.0127
    assert [row["event_date"] for row in serving["walk_forward"]["by_day"]] == [
        "2026-09-18", "2026-09-19"
    ]

    off = C._p1_direct_update_brief({"direct_update": _direct_update_block("IDENTITY")})
    assert off["serving"] is False, "a gate that is not SERVE is not serving"

    # A block that claims SERVE but cannot be served: production raises, so
    # the page says refused rather than repeating the claim.
    broken = _direct_update_block()
    broken["theta_rise"] = broken["theta_rise"][:-1]
    refused = C._p1_direct_update_brief({"direct_update": broken})
    assert refused["serving"] is False and refused["refused"], refused
    assert refused["state"] == "SERVE", "and still reports what the block claims"

    assert C._p1_direct_update_brief({})["present"] is False


def test_every_charter_161_term_has_a_reader_facing_name() -> None:
    from bayes_basket_target import p1_direct_update as DU

    brief = C._p1_direct_update_brief({"direct_update": _direct_update_block()})
    terms = brief["terms"]
    assert len(terms) == len(DU.x_layout())
    assert {row["group"] for row in terms} == {"clock", "base", "late", "mid"}
    by_name = {row["feature"]: row["label"] for row in terms}
    assert by_name["intercept"] == "截距"
    assert by_name["clock_hat_6h"] == "时钟 6h"
    # A state column and its two end-of-day repeats read as the same feature.
    word = C.P1_LIKELIHOOD_FEATURE_WORDS["drawdown_from_high_c"]
    assert by_name["drawdown_from_high_c"] == word
    assert by_name["late_x_drawdown_from_high_c"] == f"剩余 <6h × {word}"
    assert by_name["mid_x_drawdown_from_high_c"] == f"剩余 6–10h × {word}"
    # No column falls through to its code name.
    assert not [
        row for row in terms if row["label"] == row["feature"]
    ], "a term with no word is a term nobody can read"


def test_the_section_leads_with_what_is_serving_and_keeps_g_alpha_as_fallback() -> None:
    """User, 2026-09-20: 「P1 联合似然」补 161.

    Until then the section described g**alpha as the rule with no mention of
    charter 161, which is the board telling a reader that a retired
    arithmetic is live.
    """

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    start = source.index("function p1JointBoard(){")
    body = source[start:source.index("// The newest day of a daily series", start)]
    assert "A.direct_update" in body, "the section reads charter 161's block"
    assert "F.served_update" in body, "and which rule each station actually served"
    lead = body.index("第 161 条：直接更新")
    fallback = body.index("<b>第 150 条：g<sup>α</sup></b>")
    assert lead < fallback, "161 leads, 150 is the fallback under it"
    # g**alpha's own readings are kept, folded, not deleted.
    assert body.index("各档留出读数") > fallback
    assert '<details class="note" style="margin-top:14px"' in body[fallback - 200:fallback]


def test_the_receipt_says_which_update_served(tmp_path, monkeypatch) -> None:
    """A charter 161 head and a charter 150 head both say APPLIED; only the
    receipt's ``update`` tells them apart, so the board counts that."""

    conn = sqlite3.connect(tmp_path / "env.db")
    conn.execute(
        "CREATE TABLE basket_probability_envelope (checkpoint_id TEXT, stage TEXT,"
        " station_id TEXT, event_date TEXT, settlement_timezone TEXT,"
        " decision_at_utc TEXT, probabilities_json TEXT, bucket_ordinals_json TEXT,"
        " lineage_json TEXT, posterior_version TEXT, state TEXT)"
    )
    for station, receipt in (
        ("ZUUU", {"state": "APPLIED", "update": "DIRECT_LOG_UPDATE_V1", "lambda": 1.0}),
        ("ZSPD", {"state": "APPLIED", "alpha": 0.5}),
    ):
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("c-" + station, "P1", station, "2026-09-20", "Asia/Shanghai",
             "2026-09-20T02:00:00Z", json.dumps({"20": 1.0}), json.dumps({"20": 0}),
             json.dumps({"p1_observation_likelihood_json": json.dumps(receipt)}),
             "p1_joint_likelihood_v17", "COMMITTED"),
        )
    conn.commit()
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 20, 3, 0, tzinfo=C.timezone.utc))
    out = C.collect_p1_joint_likelihood(conn)
    assert out["outcome"] == {"APPLIED": 2}, "both heads say APPLIED"
    assert out["served_update"] == {"DIRECT_LOG_UPDATE_V1": 1, "G_ALPHA": 1}
    by_station = {row["station_id"]: row for row in out["stations"]}
    assert by_station["ZUUU"]["update"] == "DIRECT_LOG_UPDATE_V1"
    assert by_station["ZSPD"]["update"] == "G_ALPHA"

def test_charter_157s_station_clock_is_not_folded_into_the_fallback() -> None:
    """Charter 161 narrowed 156/157, it did not retire them.

    The alpha knots are the direct update's own clock hats
    (``p1_direct_update.KNOTS_HOURS is p1_observation_likelihood
    .ALPHA_KNOTS_HOURS``) and charter 157's per-station peak shift moves its
    clock too, so only the alpha VALUES belong under "the fallback".  Putting
    the shift inside that fold would hide a rule that is running.
    """

    from bayes_basket_target import p1_direct_update as DU
    from bayes_basket_target import p1_observation_likelihood as OL

    assert tuple(DU.KNOTS_HOURS) == tuple(float(k) for k in OL.ALPHA_KNOTS_HOURS), (
        "the knots are shared; if they part, the section has to say so"
    )
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    start = source.index("function p1JointBoard(){")
    body = source[start:source.index("// The newest day of a daily series", start)]
    clock = body.index("${clockNote}")
    fallback = body.index("<b>第 150 条：g<sup>α</sup></b>")
    assert clock < fallback, "the shared clock reads above the fallback fold"
    assert "${alphaCells}" in body and body.index("${alphaCells}") > fallback, (
        "and the alpha values read inside it"
    )

