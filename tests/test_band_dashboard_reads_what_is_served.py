"""The band section describes the tables production is applying, on their windows.

Four ways the page and the serving path had come apart, all found reading the
live store on 2026-09-15 against the rendered board:

* Accumulating HOLDS the incumbent's factors (``fit_band_reliability_table``)
  and journals an empty report.  The card took the journal's evidence, so
  P0's three and P1's five live factors printed "证据充分，已生效" beside a
  blank ratio column, under "攒证据 2/10 天" and a bar promising correction
  would *start* at ten days -- while 703 of 720 P0 and all 2,132 P1 decision
  rows that morning applied them.
* The residual audit read the whole history while the fit it sits under has
  been bounded by the served-arithmetic anchor since 2026-09-10, and filed
  every row under today's axis: 1,025 of P0's 1,225 audited station-days had
  been corrected by claim-axis tables.
* The shadow's promoted cells follow the pooled axis but were printed as
  probabilities -- a distance rung ``[1, 2)`` read "100–100%".
* Two journal outcomes (a veto, a lost write lock) rendered as "攒证据中".

Renderers are run under node from the shipped template, as in
``test_charter_105_band_dashboard_axis``: the defects were in what the
functions produced, which a source grep cannot see.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import band_reliability_store as band_store  # noqa: E402
from bayes_basket_target import center_width_store  # noqa: E402

DISTANCE = "centre_distance"
# P0's live table and the 2026-09-10 fit that set it.
HELD_FACTORS = [
    [[0.0, 1.0], 1.0],
    [[1.0, 2.0], 1.0475153905280654],
    [[2.0, 3.0], 1.0],
    [[3.0, 4.0], 0.9840320314626928],
    [[4.0, 99.0], 0.6943378762404834],
]
HELD_RATIOS = {
    "[0.0, 1.0]": (1.0663, False, "interval_spans_one"),
    "[1.0, 2.0]": (1.1175, True, None),
    "[2.0, 3.0]": (1.0565, False, "interval_spans_one"),
    "[3.0, 4.0]": (0.8059, True, None),
    "[4.0, 99.0]": (0.5940, True, None),
}


def _measured_evidence() -> dict:
    return {
        "axis": DISTANCE,
        "accumulating": False,
        "serving_day_count": 41,
        "serving_station_day_count": 1502,
        "serving_report": {
            key: {"ratio": ratio, "qualified": ok, "blocked_by": blocked}
            for key, (ratio, ok, blocked) in HELD_RATIOS.items()
        },
        "basis": {
            key: ("serving_lane" if ok else f"identity:{blocked}")
            for key, (_ratio, ok, blocked) in HELD_RATIOS.items()
        },
    }


def _accumulating_evidence() -> dict:
    """What the journal holds while the lane holds the incumbent."""

    return {
        "axis": DISTANCE,
        "accumulating": True,
        "serving_day_count": 2,
        "serving_station_day_count": 19,
        "serving_report": {},
        "basis": {
            str([float(cell[0]), float(cell[1])]): (
                "accumulating_evidence:held_incumbent"
                if factor != 1.0 else "accumulating_evidence"
            )
            for cell, factor in HELD_FACTORS
        },
    }


def _journal(conn, stage, at, outcome, sha, days, station_days, evidence):
    conn.execute(
        "INSERT INTO calibration_band_reliability_fit_journal"
        " (fit_id, stage, fitted_at_utc, band_table_sha256, serving_day_count,"
        "  serving_station_day_count, qualifying_cell_count, outcome,"
        "  factors_json, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            f"{stage}-{at}", stage, at, sha, days, station_days, 3, outcome,
            json.dumps(HELD_FACTORS), json.dumps(evidence),
        ),
    )


@pytest.fixture()
def held_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    band_store.ensure_band_table_schema(conn)
    sha = band_store.band_table_sha256(
        [((c[0], c[1]), f) for c, f in HELD_FACTORS], stage="P0", axis=DISTANCE
    )
    conn.execute(
        "INSERT INTO calibration_band_reliability_table"
        " (band_table_sha256, stage, factors_json, evidence_json,"
        "  fitted_at_utc, serving_day_count, activated, activated_at_utc, axis)"
        " VALUES (?,?,?,?,?,?,1,?,?)",
        (
            sha, "P0", json.dumps(HELD_FACTORS),
            json.dumps(_measured_evidence()), "2026-09-10T04:20:32Z", 41,
            "2026-09-10T04:20:32Z", DISTANCE,
        ),
    )
    _journal(conn, "P0", "2026-09-10T04:20:32.100565Z", "ACTIVATED", sha,
             41, 1502, _measured_evidence())
    _journal(conn, "P0", "2026-09-14T17:56:26.516072Z", "ACCUMULATING", sha,
             2, 19, _accumulating_evidence())
    return conn


def _extract(name: str) -> str:
    source = C.HTML_TEMPLATE
    start = source.index(f"function {name}(){{")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"{name}() has unbalanced braces")


def _node(payload: dict, *functions: str, call: str) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI images all carry node
        pytest.skip("node is required to run the board's renderer")
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s);\n"
        "const hours = (v) => v == null ? '-' : String(v);\n"
        + "\n".join(_extract(name) for name in functions)
        + f"\nconsole.log({call});\n"
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _render_stages(stages: list[dict]) -> str:
    return _node(
        {"stages": stages, "band_residual_audit": {}},
        "reliabilityAudit", "stages", call="stages()",
    )


def _why_column(html: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", text).strip()
        for text in re.findall(r'<td class="muted">([^<]*)</td></tr>', html)
    ]


def test_a_held_table_says_it_is_held_and_shows_the_fit_that_set_it(
    held_conn,
) -> None:
    (stage,) = [s for s in C.collect_band_stages(held_conn) if s["stage"] == "P0"]
    assert stage["held_from"] == {
        "fitted_at_utc": "2026-09-10T04:20:32.100565Z",
        "days": 41,
        "station_days": 1502,
    }
    assert stage["state"] == "攒证据 2/10 天 · 沿用 3 档"
    # The ratios behind the live factors ride out from the fit that set them.
    ratios = [b["measured_ratio"] for b in stage["bands"]]
    assert ratios == [1.0663, 1.1175, 1.0565, 0.8059, 0.5940]

    html = _render_stages([stage])
    why = _why_column(html)
    assert "证据充分，已生效" not in html
    assert why[1] == "沿用 09-10 04:20 那版，照常生效；新证据攒满再重拟"
    assert why[0] == "沿用 09-10 04:20 那版：此档不修（统计上分不清是真偏还是巧合）"
    assert "1.12×" in html and "0.59×" in html
    assert "41 天 / 1502 个站日" in html
    # The bar no longer promises a correction that is already being applied.
    assert "就会自动开始尝试修正" not in html
    assert "这 3 档保留、改值或撤回" in html


def test_an_accumulating_identity_table_with_no_measured_fit_is_unchanged() -> None:
    """A table after an axis move: identity, never measured on this axis.

    (P0-M was the live case; since charter 150 only P0 renders a band card.)
    """

    conn = sqlite3.connect(":memory:")
    band_store.ensure_band_table_schema(conn)
    identity = [[cell, 1.0] for cell, _f in HELD_FACTORS]
    sha = band_store.band_table_sha256(
        [((c[0], c[1]), 1.0) for c, _f in HELD_FACTORS],
        stage="P0", axis=DISTANCE,
    )
    evidence = _accumulating_evidence()
    evidence["basis"] = {k: "accumulating_evidence" for k in evidence["basis"]}
    conn.execute(
        "INSERT INTO calibration_band_reliability_table"
        " (band_table_sha256, stage, factors_json, evidence_json,"
        "  fitted_at_utc, serving_day_count, activated, activated_at_utc, axis)"
        " VALUES (?,?,?,?,?,?,1,?,?)",
        (sha, "P0", json.dumps(identity), json.dumps(evidence),
         "2026-09-11T00:15:40Z", 1, "2026-09-11T00:15:40Z", DISTANCE),
    )
    (stage,) = [s for s in C.collect_band_stages(conn) if s["stage"] == "P0"]
    assert stage["held_from"] is None
    assert stage["state"] == "攒证据 1/10 天"
    html = _render_stages([stage])
    assert set(_why_column(html)) == {"样本还不够，先不动"}
    # Scoped to the card: the section's prose explains holding for everyone.
    card = html[html.index('<div class="card">'):]
    assert "沿用" not in card
    assert "就会自动开始尝试修正" in card


def test_every_journal_outcome_has_its_own_label(held_conn) -> None:
    (stage,) = [s for s in C.collect_band_stages(held_conn) if s["stage"] == "P0"]
    stage["journal"] = [
        {"at": "2026-09-16T08:00:00Z", "outcome": "INCUMBENT_VETOED",
         "days": 3, "cells": 0, "sha": "aaaaaaaaaaaa"},
        {"at": "2026-09-15T08:00:00Z", "outcome": "FIT_NOT_ACTIVATED",
         "days": 3, "cells": 3, "sha": "bbbbbbbbbbbb"},
    ]
    html = _render_stages([stage])
    details = html[html.index("<details"):]
    assert "现役被否决·退役" in details
    assert "拟合成、未激活" in details
    assert "攒证据中" not in details


def test_a_vetoed_stage_does_not_read_as_never_built() -> None:
    conn = sqlite3.connect(":memory:")
    band_store.ensure_band_table_schema(conn)
    conn.execute(
        "INSERT INTO calibration_band_reliability_table"
        " (band_table_sha256, stage, factors_json, evidence_json,"
        "  fitted_at_utc, serving_day_count, activated, activated_at_utc,"
        "  axis, retired_at_utc) VALUES (?,?,?,?,?,?,0,?,?,?)",
        ("c" * 64, "P0", json.dumps(HELD_FACTORS), "{}",
         "2026-09-10T04:20:38Z", 36, "2026-09-10T04:20:38Z", DISTANCE,
         "2026-09-16T08:00:00Z"),
    )
    (stage,) = [s for s in C.collect_band_stages(conn) if s["stage"] == "P0"]
    assert stage["state"] == "现役 2026-09-16 被否决退役 · 按恒等服务"
    assert stage["health"] == "amber"
    assert stage["axis"] == DISTANCE


def test_a_stage_charter_150_serves_no_table_for_is_not_an_alert() -> None:
    """P0-M and P1 serve no band table by design (charter 150): the refit
    retired theirs on 2026-09-18 and fits nothing for them since.  That read
    as an amber "vetoed, serving identity" on the banner every render; it is
    the design, so it says so and stays off the banner."""

    conn = sqlite3.connect(":memory:")
    band_store.ensure_band_table_schema(conn)
    for stage, sha in (("P0-M", "e" * 64), ("P1", "f" * 64)):
        conn.execute(
            "INSERT INTO calibration_band_reliability_table"
            " (band_table_sha256, stage, factors_json, evidence_json,"
            "  fitted_at_utc, serving_day_count, activated, activated_at_utc,"
            "  axis, retired_at_utc) VALUES (?,?,?,?,?,?,0,?,?,?)",
            (sha, stage, json.dumps(HELD_FACTORS), "{}",
             "2026-09-10T04:20:38Z", 36, "2026-09-10T04:20:38Z", DISTANCE,
             "2026-09-18T20:16:02Z"),
        )
    stages = {s["stage"]: s for s in C.collect_band_stages(conn)}
    for stage in ("P0-M", "P1"):
        assert stage not in band_store.BAND_SERVED_STAGES
        assert stages[stage]["health"] == "muted"
        assert stages[stage]["state"].startswith("宪章 150")
    alerts = C._band_stage_alerts(list(stages.values()))
    assert not [a for a in alerts if a["label"].endswith(("P0-M", "P1"))]


def test_a_vetoed_stage_keeps_the_axis_its_cells_were_keyed_on() -> None:
    """The axis lives on the table, so a veto takes it away with the row.

    Every caller of ``_band_axis_by_stage`` falls back to ``claim``; after
    P0's table was retired on 2026-09-15 that put a distance-keyed stage
    back on the 5% claim grid in the residual audit -- charter 105's bug,
    re-entered through the retirement door.
    """

    conn = sqlite3.connect(":memory:")
    band_store.ensure_band_table_schema(conn)
    conn.execute(
        "INSERT INTO calibration_band_reliability_table"
        " (band_table_sha256, stage, factors_json, evidence_json,"
        "  fitted_at_utc, serving_day_count, activated, activated_at_utc,"
        "  axis, retired_at_utc) VALUES (?,?,?,?,?,?,0,?,?,?)",
        ("d" * 64, "P0", json.dumps(HELD_FACTORS), "{}",
         "2026-09-10T04:20:32Z", 41, "2026-09-10T04:20:32Z", DISTANCE,
         "2026-09-15T18:26:19Z"),
    )
    assert C._band_axis_by_stage(conn)["P0"] == DISTANCE


def test_the_audit_rows_read_the_fits_window_and_the_station_court_does_not(
    monkeypatch,
) -> None:
    """Stage rows on the anchor; the per-station court on the whole history.

    The court feeds the go-live page's claim-honesty gate, so moving it onto
    a window that restarts at every arithmetic change would quietly change
    which stations that page proposes releasing.
    """

    anchor = "2026-09-14T05:52:13Z"
    calls: list = []

    def lane(stage: str, bound: str | None, pre_band: bool) -> dict:
        station = "KNEW" if bound else "KOLD"
        event = "2026-09-14" if bound else "2026-08-20"
        if pre_band:
            rows = [(0.20, 0, 10.0), (0.50, 1, 11.0), (0.30, 0, 12.0)]
        else:
            rows = [(0.18, 0, 10.0), (0.55, 1, 11.0), (0.27, 0, 12.0)]
        return {(station, event): [rows]}

    def fake_lane(conn, truth, *, stage, pre_band=True, min_decision_at_utc=None):
        calls.append((stage, pre_band, min_decision_at_utc))
        return lane(stage, min_decision_at_utc, pre_band)

    monkeypatch.setattr(band_store, "serving_lane_by_station_day", fake_lane)
    monkeypatch.setattr(center_width_store, "_truth", lambda conn: {})
    monkeypatch.setattr(
        center_width_store, "served_arithmetic_anchor",
        lambda conn, *, stage, known_floor=None: anchor,
    )
    conn = sqlite3.connect(":memory:")
    band_store.ensure_band_table_schema(conn)
    for stage in ("P0", "P0-M", "P1"):
        conn.execute(
            "INSERT INTO calibration_band_reliability_table"
            " (band_table_sha256, stage, factors_json, evidence_json,"
            "  fitted_at_utc, serving_day_count, activated, activated_at_utc,"
            "  axis) VALUES (?,?,?,?,?,?,1,?,?)",
            (stage.ljust(64, "0"), stage, json.dumps(HELD_FACTORS), "{}",
             "2026-09-10T04:20:32Z", 41, "2026-09-10T04:20:32Z", DISTANCE),
        )

    audit = C.collect_band_residual_audit(conn)

    for stage in audit["stages"]:
        assert stage["since"] == anchor
        assert stage["station_days"] == 1
        assert stage["day_count"] == 1
    assert {(stage, bound) for stage, _pre, bound in calls} == {
        (stage, bound) for stage in ("P0", "P0-M", "P1")
        for bound in (None, anchor)
    }
    # The court is the unbounded lane's station only, once per stage.
    assert [(row["station"], row["n"]) for row in audit["stations"]] == [
        ("KOLD", 3)
    ]

    html = _node(
        {"band_residual_audit": audit}, "reliabilityAudit",
        call="reliabilityAudit()",
    )
    assert "自 09-14 05:52Z 起" in html
    assert "同窗同轴" not in html
    assert "全历史" in html


def test_an_empty_stage_says_why_and_does_not_take_the_court_with_it() -> None:
    audit = {
        "stages": [{
            "stage": "P0-M", "axis": DISTANCE, "since": "2026-09-14T05:52:13Z",
            "day_count": 0, "station_days": 0, "skipped_station_days": 48,
            "rows": [],
        }],
        "stations": [{
            "station": "KSFO", "station_label": "", "n": 74,
            "claimed": 0.676, "realized": 0.527, "ratio": 0.779,
            "significant": True,
        }],
    }
    html = _node(
        {"band_residual_audit": audit}, "reliabilityAudit",
        call="reliabilityAudit()",
    )
    assert "band 没有改动过任何站日" in html
    assert "KSFO" in html


def _shadow_payload(axis: str, band: list[float]) -> dict:
    return {"band_shadow": {
        "stages": [{
            "stage": "P0", "meaning": "", "axis": axis,
            "fitted_at": "2026-09-10T04:25:28Z", "fit_age_hours": 1,
            "sha": "bc14adc5d3dc", "serving_days": 41, "station_days": 1502,
            "cell_count": 405, "inherited_cell_count": 96,
            "forward_nll_gain": 3.59, "levels": [],
            "promoted": [{
                "station": None, "group": "warm_near",
                "group_label": "偏暖邻桶", "band": band, "factor": 1.1248,
            }],
            "journal": [],
        }],
        "gates": {"min_claimed_mass": 8.0, "min_days": 8, "min_rows": 30,
                  "fdr_q": 0.1},
        "distance": [],
    }}


def test_a_distance_shadow_cell_prints_as_a_rung_not_a_percentage() -> None:
    html = _node(
        _shadow_payload(DISTANCE, [1.0, 2.0]), "bandShadow",
        call="bandShadow()",
    )
    assert "离峰 1 格" in html
    assert "100–100%" not in html
    assert "<th>离峰距离</th>" in html

    claim = _node(
        _shadow_payload("claim", [0.2, 0.25]), "bandShadow",
        call="bandShadow()",
    )
    assert "20–25%" in claim
    assert "<th>概率档</th>" in claim


def test_the_card_names_decision_rows_still_on_an_older_pinned_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_band_reliability_table"
        " (band_table_sha256 TEXT, axis TEXT)"
    )
    active, old_claim = "a" * 64, "b" * 64
    conn.executemany(
        "INSERT INTO calibration_band_reliability_table VALUES (?,?)",
        [(active, DISTANCE), (old_claim, "claim")],
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history"
        " (stage TEXT, decision_at_utc TEXT, checkpoint_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_envelope"
        " (envelope_id TEXT, lineage_json TEXT)"
    )
    now = datetime.now(timezone.utc)
    rows = [(active, 5), (old_claim, 2), (None, 1)]
    index = 0
    for sha, count in rows:
        for _ in range(count):
            index += 1
            decided = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            lineage = {} if sha is None else {
                "band_reliability_json": json.dumps(
                    {"state": "APPLIED", "band_table_sha256": sha}
                )
            }
            conn.execute(
                "INSERT INTO basket_probability_bridge_history VALUES (?,?,?)",
                ("P0", decided, f"env-{index}"),
            )
            conn.execute(
                "INSERT INTO basket_probability_envelope VALUES (?,?)",
                (f"env-{index}", json.dumps(lineage)),
            )
    # A day-old row is outside the window.
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES (?,?,?)",
        ("P0", (now - timedelta(hours=30)).isoformat().replace("+00:00", "Z"),
         "env-old"),
    )
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES (?,?)",
        ("env-old", json.dumps({"band_reliability_json": json.dumps(
            {"state": "APPLIED", "band_table_sha256": old_claim})})),
    )

    served = C._band_served_versions(conn, stage="P0", active_sha=active)
    assert served == {
        "hours": 24, "rows": 8, "active_rows": 5, "unreceipted_rows": 1,
        "others": [{"sha": "b" * 12, "axis": "claim", "rows": 2}],
    }
    stage = {
        "stage": "P0", "meaning": "", "state": "3 档生效", "health": "green",
        "table_sha": "a" * 12, "fit_age_hours": 1, "serving_days": 41,
        "min_days": 10, "station_days": 1502, "accumulating": False,
        "active_cells": 3, "held_from": None, "served_versions": served,
        "axis": DISTANCE, "bands": [], "journal": [],
    }
    html = _render_stages([stage])
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))
    assert "近 24h 决策行 5/8 用这一版" in text
    assert "2 行仍用旧 artifact 钉住的版本" in text
    assert "声称概率轴 2 行" in text
    assert "1 行无 band 回执" in text

    # And a stage whose rows are all on the active table says nothing.
    quiet = dict(stage, served_versions={
        "hours": 24, "rows": 5, "active_rows": 5, "unreceipted_rows": 0,
        "others": [],
    })
    assert "决策行" not in _render_stages([quiet])
