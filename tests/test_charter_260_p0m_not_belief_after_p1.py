"""Charter 260: once a P1 stands for a station-day, that day's P0-M is not an
independent belief (user ruling 2026-10-09 「裁决」 on 「P0-M 在 P1 存在后的当日决策
是否不再作为独立信念服务」).  Readers take P1 > P0-M > P0 at the instant; the width
lane does not count a superseded P0-M decision as evidence."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bayes_basket_target import stage_authority as SA  # noqa: E402
from bayes_basket_target import copy_follow_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_257_copy_follow_v2 import ED, ST, _served  # noqa: E402
from tests.test_tiebreak_lane import store  # noqa: E402,F401 -- fixture


def test_the_belief_is_the_most_informed_stage_standing_not_the_newest_row(store) -> None:  # noqa: F811
    _served(store, {"27°C": 0.2, "28°C": 0.5, "29°C": 0.3}, stage="P0", decided="2026-09-18T06:00:00Z", hid="h_p0")
    assert SA.served_belief_row(store, station_id=ST, event_date=ED, at_utc="2026-09-18T05:00:00Z") is None
    assert SA.served_belief_row(store, station_id=ST, event_date=ED, at_utc="2026-09-18T07:00:00Z")["stage"] == "P0"
    _served(store, {"27°C": 0.2, "28°C": 0.5, "29°C": 0.3}, stage="P1", decided="2026-09-18T11:00:00Z", hid="h_p1")
    _served(store, {"27°C": 0.1, "28°C": 0.3, "29°C": 0.6}, stage="P0-M", decided="2026-09-18T11:40:00Z", hid="h_pm")
    belief = SA.served_belief_row(store, station_id=ST, event_date=ED, at_utc="2026-09-18T12:00:00Z")
    assert (belief["stage"], belief["history_id"]) == ("P1", "h_p1"), "the later P0-M row is not the belief"
    belief = SA.served_belief_row(store, station_id=ST, event_date=ED, at_utc="2026-09-18T11:50:00Z")
    assert belief["stage"] == "P1"


def test_the_copy_follow_lane_reads_the_belief_stage_and_refuses_a_stage_it_may_not_read(store) -> None:  # noqa: F811
    _served(store, {"27°C": 0.2, "28°C": 0.5, "29°C": 0.3}, stage="P0", decided="2026-09-18T06:00:00Z", hid="h_p0")
    _served(store, {"27°C": 0.1, "28°C": 0.3, "29°C": 0.6}, stage="P0-M", decided="2026-09-18T08:00:00Z", hid="h_pm")
    got = lane.served_side_probability(store, station_id=ST, event_date=ED, bucket="29°C", side="YES",
                                       stages=["P0", "P0-M"], now_utc="2026-09-18T09:00:00Z")
    assert got["state"] == "ok" and got["stage"] == "P0-M" and got["p"] == pytest.approx(0.6, abs=1e-3)
    _served(store, {"27°C": 0.05, "28°C": 0.9, "29°C": 0.05}, stage="P1", decided="2026-09-18T10:00:00Z", hid="h_p1")
    got = lane.served_side_probability(store, station_id=ST, event_date=ED, bucket="29°C", side="YES",
                                       stages=["P0", "P0-M", "P1"], now_utc="2026-09-18T11:00:00Z")
    assert got["stage"] == "P1" and got["p"] == pytest.approx(0.05, abs=2e-3), "P1 stands: it is the belief"
    held = lane.served_side_probability(store, station_id=ST, event_date=ED, bucket="29°C", side="YES",
                                        stages=["P0", "P0-M"], now_utc="2026-09-18T11:00:00Z")
    assert held["state"] == "stage_not_allowed:P1", "never a fall back to the superseded P0-M"
    assert "P1" in strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS["model_stages"]


def test_the_width_lane_drops_p0m_rows_a_p1_had_superseded(store) -> None:  # noqa: F811
    _served(store, {"27°C": 0.2, "28°C": 0.5, "29°C": 0.3}, stage="P1", decided="2026-09-18T10:00:00Z", hid="h_p1")
    first = SA.first_p1_decisions(store)
    assert first == {(ST, ED): "2026-09-18T10:00:00Z"}
    rows = [(ST, ED, "2026-09-18T08:00:00Z", "x"), (ST, ED, "2026-09-18T10:00:00Z", "y"), (ST, ED, "2026-09-18T15:00:00Z", "z"),
            ("KAAA", ED, "2026-09-18T15:00:00Z", "w")]
    kept = SA.drop_superseded_p0m(rows, stage="P0-M", first_p1=first)
    assert [r[3] for r in kept] == ["x", "w"], "at or after the first P1 the day's P0-M is superseded; other days untouched"
    assert SA.drop_superseded_p0m(rows, stage="P0", first_p1=first) == rows, "only P0-M is superseded"
    assert SA.first_p1_decisions(store, min_decision_at_utc="2026-09-21T00:00:00Z") == {}


def test_the_charter_and_the_lane_wiring() -> None:
    root = Path(__file__).resolve().parents[1]
    charter = (root / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter[charter.index("\n260. ") + 1:]
    for phrase in ("P1 存在", "独立信念", "stage_authority", "serving_cells_by_day", "P1 > P0-M > P0"):
        assert phrase in clause, phrase
    import ast
    from bayes_basket_target import center_width_store
    src = Path(center_width_store.__file__).read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "serving_cells_by_day")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert {"first_p1_decisions", "p0m_superseded"} <= names
