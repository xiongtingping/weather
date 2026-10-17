"""Charter 231 补记二: the Proper lane opens every belief stage, and the
realisation layer's b is fitted, stored and read per stage.

User ruling 2026-10-10 「把 P0-M、P1 阶段也放开」 (after 「也是人工定的，按照proper
线本来的设计来」 dropped the >36 h window).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bayes_basket_target import proper_bet_lane as lane  # noqa: E402
from bayes_basket_target import realisation_store as rstore  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_231_proper_bet_lane import (  # noqa: E402
    DECIDED, ED, ST, _fact, _run, _seed, _snapshot, _ladder,
)
from tests.test_charter_254_realisation_layer_shadow import _artifact  # noqa: E402
from tests.test_tiebreak_lane import store  # noqa: E402,F401 -- ``store`` is a fixture

ALL = ["P0", "P0-M", "P1"]


def test_the_default_opens_every_stage() -> None:
    assert strategy_lanes.PROPER_BET_DEFAULT_SETTINGS["opening_stages"] == ALL
    assert strategy_lanes.opening_stages(strategy_lanes.PROPER_BET_DEFAULT_SETTINGS) == frozenset(ALL)


def test_the_belief_stage_is_the_most_informed_not_the_newest(store) -> None:
    _snapshot(store, "snap_a", DECIDED, _ladder(), 1)
    _fact(store, stage="P1", fact_id="a1", decided="2026-09-18T11:00:00.000Z", snapshot_id="snap_a")
    # P0-M keeps being compiled after the day's first P1 (charter 260): it is newer, and not the belief.
    _fact(store, stage="P0-M", fact_id="a2", decided="2026-09-18T11:30:00.000Z", snapshot_id="snap_a")
    assert lane._latest_stage(store, ST, ED) == "P0-M"
    assert lane._belief_stage(store, ST, ED) == "P1"
    picked = lane.belief_stage_decisions(store, now_utc="2026-09-18T12:00:00Z")
    assert [(d["stage"], d["decision_at_utc"]) for d in picked] == [("P1", "2026-09-18T11:00:00.000Z")]


@pytest.mark.parametrize("stage", ["P0-M", "P1"])
def test_a_p0m_or_p1_station_day_is_planned_when_its_stage_is_open(store, stage) -> None:
    _seed(store, stage=stage)
    sent: list = []
    receipt = _run(store, sent, opening_stages=ALL)
    assert receipt["planned"] == 1 and sent
    row = store.execute(f"SELECT stage FROM {lane.EVALUATION_TABLE}").fetchone()
    assert row[0] == stage


def test_a_closed_stage_still_waits(store) -> None:
    _seed(store, stage="P1")
    receipt = _run(store, [], opening_stages=["P0"])
    assert receipt["planned"] == 0 and receipt["evaluated"] == 0


def test_a_plan_closes_when_its_stage_moves_on_and_the_next_stage_plans(store) -> None:
    _seed(store, stage="P0-M")
    sent: list = []
    assert _run(store, sent, opening_stages=ALL)["planned"] == 1
    _snapshot(store, "snap_p1", "2026-09-18T12:04:00.000Z", _ladder(), 2)
    _fact(store, stage="P1", fact_id="p1", decided="2026-09-18T12:04:00.000Z", snapshot_id="snap_p1")
    receipt = _run(store, sent, now="2026-09-18T12:04:30.000Z", opening_stages=ALL)
    reasons = [c["reason"] for c in receipt.get("closed_plans") or []]
    assert "stage_left_p0m:P1" in reasons
    stages = {r[0] for r in store.execute(f"SELECT stage FROM {lane.EVALUATION_TABLE}")}
    assert stages == {"P0-M", "P1"}


# ---------------------------------------------------------------------------
# The realisation layer per stage.


def test_artifacts_are_kept_per_stage(tmp_path) -> None:
    p0 = rstore.write_artifact({**_artifact(), "stage": "P0"}, tmp_path)
    p1 = rstore.write_artifact({**_artifact(theta=(1.0, 0, 0, 0, 0, 0, 0)), "stage": "P1"}, tmp_path)
    assert p0.name == "active.json" and p1.name == "active_p1.json"
    assert rstore.load_active(tmp_path)["stage"] == "P0"
    assert rstore.load_active(tmp_path, stage="P1")["theta"][0] == 1.0
    assert rstore.load_active(tmp_path, stage="P0-M") is None
    # P0's history keeps its names; another stage's carry its slug, pruned apart.
    assert len(list((tmp_path / "history").glob("fit_[0-9]*.json"))) == 1
    assert len(list((tmp_path / "history").glob("fit-p1_*.json"))) == 1
    with pytest.raises(ValueError, match="realisation_stage_unknown"):
        rstore.active_name("P2")


def test_b_is_read_only_under_its_own_stage(store) -> None:
    wrong = {**_artifact(), "stage": "P0", "rule": "x"}
    read = rstore.belief_vector(store, station_id=ST, event_date=ED, as_of_utc=DECIDED, p_raw={"a": 1.0},
                                book_buckets=[], artifact=wrong, stage="P1")
    assert read["state"] == "fit_stage_mismatch"


def test_the_p0m_population_drops_decisions_after_the_first_p1(store, monkeypatch) -> None:
    calls: list = []

    def fake_first(conn, *, min_decision_at_utc=None):
        calls.append(min_decision_at_utc)
        return {(ST, ED): "2026-09-18T11:00:00.000Z"}

    monkeypatch.setattr("bayes_basket_target.stage_authority.first_p1_decisions", fake_first)
    monkeypatch.setattr("bayes_basket_target.label_knowability.knowable_labels",
                        lambda conn: {(ST, ED): {"bucket": "28°C", "known_at": "2026-09-19T00:00:00Z"}})
    _snapshot(store, "snap_m", DECIDED, _ladder(), 1)
    _fact(store, stage="P0-M", fact_id="m1", decided="2026-09-18T10:00:00.000Z", snapshot_id="snap_m")
    _fact(store, stage="P0-M", fact_id="m2", decided="2026-09-18T11:30:00.000Z", snapshot_id="snap_m")
    _decisions, counts = rstore.population(store, cutoff_utc="2026-09-20T00:00:00Z", stage="P0-M")
    assert calls and counts["stage"] == "P0-M" and counts["facts_read"] == 2
    assert counts["superseded_by_p1"] == 1
    _decisions, p0_counts = rstore.population(store, cutoff_utc="2026-09-20T00:00:00Z", stage="P0")
    assert p0_counts["facts_read"] == 0


def test_the_lanes_file_opens_every_stage() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = strategy_lanes.lane_settings(strategy_lanes.LANE_PROPER_BET, root / "config" / "trading" / "strategy_lanes_v1.json")
    assert cfg["opening_stages"] == ALL


def test_the_population_reuses_each_decisions_inputs_and_reads_labels_fresh(store, monkeypatch) -> None:
    """2026-10-17 (「兑现层三项重拟每次超时、结果多日未更新」): a decision's
    ladder inputs are kept between fits, so only new decisions read the store;
    the label is read every time, so a truth revision still lands."""

    label = {"bucket": "28°C"}
    monkeypatch.setattr("bayes_basket_target.label_knowability.knowable_labels",
                        lambda conn: {(ST, ED): {"bucket": label["bucket"], "known_at": "2026-09-19T00:00:00Z"}})
    _snapshot(store, "snap_c", "2026-09-16T10:00:00.000Z", _ladder(), 1)
    _fact(store, stage="P0", fact_id="c1", decided="2026-09-16T10:00:00.000Z", snapshot_id="snap_c")
    first, counts = rstore.population(store, cutoff_utc="2026-09-20T00:00:00Z", stage="P0")
    assert counts["inputs_cached"] == 0

    def no_read(*_a, **_k):
        raise AssertionError("a cached decision must not read the store again")
    monkeypatch.setattr(rstore, "_decision_inputs", no_read)
    second, counts = rstore.population(store, cutoff_utc="2026-09-20T00:00:00Z", stage="P0")
    assert counts["inputs_cached"] == counts["facts_read"] - counts["no_label"] - counts["inside_window"]
    assert [d["y"] for d in second] == [d["y"] for d in first]
    assert all((a["terms"] == b["terms"]).all() for a, b in zip(first, second))
    assert first, "the fixture decision enters the population"
    label["bucket"] = "27°C"
    third, _ = rstore.population(store, cutoff_utc="2026-09-20T00:00:00Z", stage="P0")
    assert [d["y"] for d in third] != [d["y"] for d in first], "a revised label is read fresh"
