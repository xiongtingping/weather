"""Charter 254: the realisation layer in shadow (user ruling 2026-10-05
「上影子」, and 「前端增加对应显式看板」).

The layer ``b = P(Y | p, q)`` is a seven-number log pool over the ladder;
it is fitted nightly on a read-only connection and judged walk-forward by a
reliability gate; the Proper and far-tail lanes record, for every leg they
write down, ``b``, its band and the edge on the leg's own price -- and decide
exactly as before.  The board shows the fit and the legs.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from bayes_basket_target import proper_bet_lane, realisation_layer as layer
from bayes_basket_target import realisation_store as rstore
from bayes_basket_target import tail_short_lane
import tests.test_charter_249_proper_market_mode_yes as proper
import tests.test_charter_251_tail_short_model_filter as tails
from tests.test_charter_231_proper_bet_lane import _intents as proper_intents, _seed as proper_seed
from tests.test_tiebreak_lane import store  # noqa: F401 -- the fixture

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The kernel.


def test_the_features_on_one_ladder() -> None:
    p = [0.01, 0.05, 0.30, 0.40, 0.20, 0.04, 0.0]
    q = [0.02, 0.10, 0.25, 0.33, 0.20, 0.07, 0.03]
    f = layer.features(p, q)
    assert f["terms"].shape == (7, len(layer.PARAM_NAMES))
    assert f["mode"] == 3
    # floored once, the served floor (charter 224)
    assert f["p"].min() > 0 and math.isclose(f["p"].sum(), 1.0)
    assert np.allclose(f["terms"][:, 0], np.log(f["q"]))
    assert list(f["terms"][:, 5]) == [0, 0, 0, 1, 0, 0, 0] and list(f["terms"][:, 6]) == [0, 0, 1, 0, 1, 0, 0]
    # one centre agreement switch: exactly one of the two weather pairs is live
    live = np.abs(f["terms"][:, 1:3]).sum() > 0, np.abs(f["terms"][:, 3:5]).sum() > 0
    assert live == ((True, False) if f["centres_agree"] else (False, True))
    # the market at theta = START is the floored market itself
    assert np.allclose(layer.belief(f["terms"], layer.START), f["q"])
    # the lower rung wins a tie for the mode
    assert layer.features([0.1] * 4, [0.3, 0.3, 0.2, 0.2])["mode"] == 0


def test_the_stretch_lands_on_its_targets() -> None:
    v = np.array([0.02, 0.08, 0.2, 0.4, 0.2, 0.08, 0.02])
    assert layer.moments(layer.shifted_to(v, 4.0))[0] == pytest.approx(4.0, abs=1e-5)
    assert layer.moments(layer.matched_to(v, 2.5, 1.6)) == pytest.approx((2.5, 1.6), abs=1e-4)


def _synthetic(n_days=14, per_day=30, seed=3):
    """Decisions whose truth follows a known pool, so the fit has an answer."""

    rng = np.random.default_rng(seed)
    truth = np.array([1.3, 0.4, 0.3, 0.1, 0.0, 0.4, 0.3])
    out = []
    for day in range(n_days):
        for s in range(per_day):
            q = rng.dirichlet(np.full(7, 2.0))
            p = rng.dirichlet(q * 20 + 0.1)
            f = layer.features(list(p), list(q))
            b = layer.belief(f["terms"], truth)
            out.append({"terms": f["terms"], "q": f["q"], "mode": f["mode"], "centres_agree": f["centres_agree"],
                        "y": int(rng.choice(7, p=b)), "weight": 1.0, "station_id": f"S{s:02d}",
                        "event_date": f"2026-09-{day + 1:02d}", "label_known_at": f"2026-09-{day + 2:02d}T06:00:00Z"})
    return out, truth


def test_the_fit_recovers_the_pool_it_was_drawn_from() -> None:
    decisions, truth = _synthetic(n_days=20, per_day=60)
    theta = layer.fit(layer.stack(decisions))
    assert theta[0] == pytest.approx(truth[0], abs=0.15)
    assert theta[5] == pytest.approx(truth[5], abs=0.2) and theta[6] == pytest.approx(truth[6], abs=0.2)
    assert layer.log_loss(layer.stack(decisions), theta) <= layer.log_loss(layer.stack(decisions), layer.START)


def test_the_gate_reads_reliability_cells_and_the_line() -> None:
    decisions, _ = _synthetic(n_days=14, per_day=40)
    forward = rstore.walk_forward(decisions)
    # a day is predicted from days at least two before it: the first eight have no prefix of eight
    assert forward["days"][0] == "2026-09-10" and len(forward["days"]) == 5
    gate = rstore.calibration_gate(decisions, forward)
    assert gate["state"] == rstore.STATE_ACCUMULATING          # five scored days < eight
    decisions, _ = _synthetic(n_days=20, per_day=40)
    gate = rstore.calibration_gate(decisions, rstore.walk_forward(decisions))
    assert gate["state"] in (rstore.STATE_ADMITTED, rstore.STATE_REFUSED)
    assert gate["cells_allowed"] == rstore._binomial_allowance(gate["cells_judged"], 0.05, 0.95)
    assert {"layer", "market", "market_only_pool", "layer_minus_market", "layer_minus_market_only_pool"} <= set(gate["logscore"])
    assert all(c["judged"] == (c["buckets"] >= rstore.MIN_BIN_BUCKETS) for c in gate["cells"])


def test_the_binomial_allowance() -> None:
    assert rstore._binomial_allowance(24, 0.05, 0.95) == 3
    assert rstore._binomial_allowance(0, 0.05, 0.95) == 0


def test_the_edge_on_each_side() -> None:
    yes = layer.leg_edge(side="YES", b=0.45, b_low=0.40, b_high=0.50, price=0.38, fee_per_share=0.0)
    assert yes == pytest.approx({"edge": 0.07, "edge_sure": 0.02})
    no = layer.leg_edge(side="NO", b=0.02, b_low=0.01, b_high=0.03, price=0.96, fee_per_share=0.002)
    assert no == pytest.approx({"edge": 0.018, "edge_sure": 0.008})
    with pytest.raises(ValueError):
        layer.leg_edge(side="MAYBE", b=0.5, b_low=0.5, b_high=0.5, price=0.5, fee_per_share=0.0)
    assert rstore.taker_fee_per_share(0.96, 0.05) == pytest.approx(0.05 * 0.96 * 0.04)


# ---------------------------------------------------------------------------
# The artifact.


def _artifact(theta=(1.2, 0.5, 0.4, 0.2, 0.1, 0.4, 0.4), state=rstore.STATE_ADMITTED):
    return {"schema": rstore.SCHEMA, "fit_id": "realisation_test", "fitted_at_utc": "2026-09-18T00:00:00Z",
            "theta": list(theta), "draws": [list(np.array(theta) + d) for d in (-0.05, 0.0, 0.05)],
            "gate": {"state": state}, "code_sha256": rstore.code_sha256()}


def test_the_artifact_round_trips_and_ages(tmp_path) -> None:
    path = rstore.write_artifact(_artifact(), tmp_path)
    assert path.name == rstore.ACTIVE_NAME and len(list((tmp_path / "history").glob("fit_*.json"))) == 1
    loaded = rstore.load_active(tmp_path)
    assert loaded["theta"] == pytest.approx([1.2, 0.5, 0.4, 0.2, 0.1, 0.4, 0.4])
    from datetime import UTC, datetime
    assert rstore.artifact_is_current(loaded, now=datetime(2026, 9, 18, 12, tzinfo=UTC))
    assert not rstore.artifact_is_current(loaded, now=datetime(2026, 9, 19, 2, tzinfo=UTC))
    assert not rstore.artifact_is_current({**loaded, "code_sha256": "other"}, now=datetime(2026, 9, 18, 1, tzinfo=UTC))


# ---------------------------------------------------------------------------
# The lanes' shadow legs.


def _shadow(conn):
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {rstore.SHADOW_TABLE} ORDER BY judged_at_utc, kind")]
    conn.row_factory = None
    return rows


def test_a_proper_plan_and_its_rung_are_read_in_shadow(store, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(proper.base, "MODEL", dict(proper.WARM))
    proper_bet_lane.realisation_store.write_artifact(_artifact(), proper_bet_lane.realisation_store.ARTIFACT_DIR)
    proper_seed(store)
    sent: list = []
    receipt = proper._run(store, sent)
    assert receipt["planned"] == 1 and len(sent) == 1        # the lane decided as it always does
    rows = _shadow(store)
    assert [r["kind"] for r in rows] == ["plan", "rung"]
    plan, rung = rows
    assert (plan["lane"], plan["bucket"], plan["side"], plan["outcome"]) == ("proper_bet", "27°C", "YES", "planned")
    assert rung["order_intent_id"] == proper_intents(store)[0][0] and rung["outcome"] == "posting"
    for row in rows:
        assert row["state"] == "ok" and row["fit_id"] == "realisation_test" and row["fit_state"] == rstore.STATE_ADMITTED
        assert 0.0 < row["b_low"] <= row["b"] <= row["b_high"] < 1.0
        assert row["edge"] == pytest.approx(row["b"] - row["price"] - row["fee_per_share"])
        assert row["edge_sure"] == pytest.approx(row["b_low"] - row["price"])
        ladder = json.loads(row["ladder_json"])
        assert ladder["buckets"] == ["27°C", "28°C", "29°C"] and len(ladder["b"]) == 3
        assert sum(ladder["b"]) == pytest.approx(1.0, abs=1e-5)
    assert rung["price"] == pytest.approx(0.49)


def test_without_a_fit_the_legs_are_still_written(store, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(proper.base, "MODEL", dict(proper.WARM))
    proper_seed(store)
    sent: list = []
    assert proper._run(store, sent)["planned"] == 1 and len(sent) == 1
    rows = _shadow(store)
    assert {r["state"] for r in rows} == {"no_fit"}
    assert all(r["b"] is None and r["edge"] is None and r["p"] is not None for r in rows)


def test_a_failing_shadow_never_stops_the_lane(store, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(proper.base, "MODEL", dict(proper.WARM))

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(proper_bet_lane.realisation_store, "load_active", broken)
    proper_seed(store)
    sent: list = []
    assert proper._run(store, sent)["planned"] == 1 and len(sent) == 1
    assert {r["state"] for r in _shadow(store)} == {"error:RuntimeError:boom"}


def test_the_far_tail_lane_reads_its_legs_in_shadow(store) -> None:  # noqa: F811
    tail_short_lane.realisation_store.write_artifact(_artifact(), tail_short_lane.realisation_store.ARTIFACT_DIR)
    tails._seed(store)
    tails._envelope(store)
    sent: list = []
    receipt = tails._run(store, sent)
    rows = _shadow(store)
    evaluations = tails._evaluations(store)
    assert {r["kind"] for r in rows} == {"take"} and len(rows) == len(evaluations)
    for row in rows:
        assert row["lane"] == "tail_short" and row["side"] == "NO" and row["state"] == "ok"
        assert row["outcome"] == (evaluations[row["bucket"]][0] or "planned")
        assert row["fee_per_share"] == pytest.approx(0.05 * row["price"] * (1 - row["price"]))
        assert row["edge"] == pytest.approx((1 - row["b"]) - row["price"] - row["fee_per_share"])
        assert row["edge_sure"] == pytest.approx((1 - row["b_high"]) - row["price"] - row["fee_per_share"])
        assert row["envelope_id"] == "env_f1"
    planned = [r for r in rows if r["outcome"] == "planned"]
    assert len(planned) == receipt["planned"] and all(r["order_intent_id"] for r in planned)


def test_the_shadow_legs_are_scored_against_what_settled(store) -> None:  # noqa: F811
    tail_short_lane.realisation_store.write_artifact(_artifact(), tail_short_lane.realisation_store.ARTIFACT_DIR)
    tails._seed(store)
    tails._envelope(store)
    tails._run(store, [])
    store.execute(
        "CREATE TABLE IF NOT EXISTS basket_station_day_settlement_label (station_id TEXT, event_date TEXT,"
        " truth_revision_no INTEGER, settled_bucket TEXT, settled_tmax_c REAL, truth_content_sha256 TEXT,"
        " known_at_utc TEXT, ladder_decision_at_utc TEXT, created_at_utc TEXT)"
    )
    store.execute("INSERT INTO basket_station_day_settlement_label VALUES (?,?,0,'28°C',28.0,'x',"
                  "'2026-09-21T06:00:00Z',NULL,'2026-09-21T06:00:00Z')", (tails.ST, tails.ED))
    out = tail_short_lane.realisation_store.shadow_realisation(store, cutoff_utc="2026-09-22T00:00:00Z")
    assert out["available"] and out["settled_station_days"] == 1
    decided = {(r["lane"], r["side"]): r for r in out["decided"]}
    row = decided[("tail_short", "NO")]
    assert row["landed_side"] == 1.0 and row["realised"][0] > 0.0
    early = tail_short_lane.realisation_store.shadow_realisation(store, cutoff_utc="2026-09-20T00:00:00Z")
    assert early["settled_station_days"] == 0


# ---------------------------------------------------------------------------
# The wiring.


def test_the_wiring_and_the_charter() -> None:
    sweep = (ROOT / "scripts" / "board_readings_sweep.sh").read_text(encoding="utf-8")
    assert "scripts/refit_realisation_layer.py" in sweep
    board = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "def collect_realisation_layer(" in board and "realisationLayer()" in board
    assert '"realisation_layer"' in board
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    line = next(l for l in charter.splitlines() if l.startswith("254. "))
    for word in ("market_conditioned_realisation_v1", "realisation_layer_shadow", "refit_realisation_layer",
                 "active.json", "上影子", "前端增加对应显式看板", "裁决人：熊先生", "不改下单"):
        assert word in line, word
