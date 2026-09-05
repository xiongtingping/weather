"""Width tiers: one lambda per tier of the served sigma, fitted on the window
the seat rule reads as well as the bucket (user ruling, 2026-09-05).

Measured on P0's own serving cells under the served 0.75, the mean+-1 window
realises 0.81 of its claim below one rung of served sigma, 1.06 at 1.0-1.44,
1.25 at 1.44-2.0, 1.34 at 2.0-2.5 and 1.76 above three: the wider the
distribution calls itself, the more its core under-claims, and a bucket-only
fit moves by thousandths where the window moves from 1.7 to 1.0.  These tests
pin the tier function the fit and serving share, the transform's tier lookup,
the combined objective, the search, the lane's admission and persistence.
"""

from __future__ import annotations

import math
import sqlite3

import pytest

from bayes_basket_target.center_width_calibration import (
    MAX_WIDTH_LAMBDA,
    MIN_WIDTH_LAMBDA,
    WIDTH_TIER_BOUNDARIES_RUNGS,
    WIDTH_TIER_NAMES,
    apply_center_width_calibration,
    distribution_sigma_rungs_v1,
    width_tier_index_v1,
)
from bayes_basket_target import center_width_store as store

KEYS = ["23C or below", "24C", "25C", "26C", "27C", "28C", "29C or higher"]
CENTERS = [22.5, 24.0, 25.0, 26.0, 27.0, 28.0, 29.5]
NARROW = dict(zip(KEYS, [0.0, 0.02, 0.16, 0.64, 0.16, 0.02, 0.0]))
WIDE = dict(zip(KEYS, [0.12, 0.14, 0.16, 0.18, 0.16, 0.14, 0.10]))


def test_the_tier_is_read_off_the_served_sigma_by_one_function() -> None:
    assert WIDTH_TIER_BOUNDARIES_RUNGS == (1.44, 2.0)
    assert len(WIDTH_TIER_NAMES) == len(WIDTH_TIER_BOUNDARIES_RUNGS) + 1
    assert width_tier_index_v1(0.7) == 0
    assert width_tier_index_v1(1.44) == 1
    assert width_tier_index_v1(1.99) == 1
    assert width_tier_index_v1(2.0) == 2
    assert width_tier_index_v1(float("nan")) == 0
    narrow = distribution_sigma_rungs_v1(NARROW, ordered_keys=KEYS, key_centers_c=CENTERS)
    wide = distribution_sigma_rungs_v1(WIDE, ordered_keys=KEYS, key_centers_c=CENTERS)
    assert narrow < 1.44 < 2.0 < wide
    assert width_tier_index_v1(narrow) == 0 and width_tier_index_v1(wide) == 2


def test_the_transform_uses_the_tiers_lambda_and_says_so() -> None:
    lambdas = [1.0, 0.8, 0.6]
    out_n, receipt_n = apply_center_width_calibration(
        NARROW, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS,
        key_centers_c=CENTERS, width_tier_lambdas=lambdas,
    )
    out_w, receipt_w = apply_center_width_calibration(
        WIDE, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS,
        key_centers_c=CENTERS, width_tier_lambdas=lambdas,
    )
    assert receipt_n["width_tier"] == 0 and receipt_n["width_tier_lambda"] == 1.0
    assert receipt_n["width_lambda"] == pytest.approx(1.0)
    assert receipt_n["pooled_width_lambda"] == pytest.approx(0.75)
    assert receipt_w["width_tier"] == 2 and receipt_w["width_tier_lambda"] == 0.6
    assert receipt_w["width_lambda"] == pytest.approx(0.6)
    # The narrow tier at lambda 1.0 is an identity; the wide one narrowed.
    assert all(out_n[k] == pytest.approx(NARROW[k]) for k in KEYS)
    assert distribution_sigma_rungs_v1(out_w, ordered_keys=KEYS, key_centers_c=CENTERS) < distribution_sigma_rungs_v1(WIDE, ordered_keys=KEYS, key_centers_c=CENTERS)
    # No tiers: the scalar path, receipt says so.
    _, plain = apply_center_width_calibration(
        WIDE, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert plain["width_tier"] is None and plain["width_lambda"] == pytest.approx(0.75)
    with pytest.raises(ValueError):
        apply_center_width_calibration(
            WIDE, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS,
            key_centers_c=CENTERS, width_tier_lambdas=[1.0, 0.8],
        )
    with pytest.raises(ValueError):
        apply_center_width_calibration(
            WIDE, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS,
            key_centers_c=CENTERS, width_tier_lambdas=[1.0, 0.8, MIN_WIDTH_LAMBDA - 0.1],
        )


def _cell(dist: dict[str, float], settled: str) -> dict:
    return {
        "probabilities": dict(dist), "ordered_keys": KEYS, "key_centers_c": CENTERS,
        "settled_bucket": settled, "excluded_buckets": (), "event_date": "2026-09-01",
    }


def test_the_objective_scores_the_window_the_seat_rule_reads() -> None:
    cell = _cell(WIDE, "26C")
    bucket, window, claim, hit = store._cell_scores(cell, delta_c=0.0, width_lambda=1.0)
    assert hit == 1 and claim == pytest.approx(0.16 + 0.18 + 0.16)
    assert bucket == pytest.approx(-math.log(0.18))
    assert window == pytest.approx(-math.log(0.50))
    miss = _cell(WIDE, "23C or below")
    _, window_miss, _, hit_miss = store._cell_scores(miss, delta_c=0.0, width_lambda=1.0)
    assert hit_miss == 0 and window_miss == pytest.approx(-math.log(0.50))
    scores = store._mean_scores([cell, miss], delta_c=0.0, width_lambda=1.0)
    assert scores["objective"] == pytest.approx(scores["bucket"] + store.WINDOW_TERM_WEIGHT * scores["window"])
    assert scores["containment"] == pytest.approx(1 / (0.5 + 0.5))


def test_the_search_gives_a_flat_tier_its_own_lambda() -> None:
    # Wide days whose truth always lands in the core want narrowing; narrow
    # days whose truth scatters do not.  The tiers are fitted apart.
    cells = [_cell(WIDE, "26C") for _ in range(12)] + [_cell(WIDE, "27C") for _ in range(6)]
    cells += [_cell(NARROW, k) for k in ("24C", "25C", "27C", "28C", "26C", "26C")]
    delta, lambdas, objective = store._search_tiers(cells, pooled_lambda=0.9)
    assert len(lambdas) == len(WIDTH_TIER_BOUNDARIES_RUNGS) + 1
    assert lambdas[2] < lambdas[0]
    assert lambdas[1] == 0.9        # no mid-tier cells: the pooled lambda
    assert all(MIN_WIDTH_LAMBDA <= v <= MAX_WIDTH_LAMBDA for v in lambdas)
    assert objective > 0.0


def test_the_lane_admits_on_the_combined_objective_without_bucket_regression(monkeypatch) -> None:
    forward = [_cell(WIDE, "26C") for _ in range(10)] + [_cell(NARROW, "26C") for _ in range(4)]
    lane = store._width_tier_lane(
        forward=forward, incumbent=(0.0, 1.0), incumbent_floor=0.0, incumbent_tiers=None,
        pooled=(0.0, 1.0), pooled_floor=0.0, tiered=(0.0, (1.0, 1.0, 0.6)),
    )
    assert lane["forward_objective_gain_vs_pooled"] > 0
    assert lane["measured_admitted"] is True
    assert lane["state"] == "SHADOW_ADMITTED"       # default mode is shadow
    assert lane["admitted"] is False
    assert [t["tier"] for t in lane["per_tier"]] == list(WIDTH_TIER_NAMES)
    monkeypatch.setattr(store, "WIDTH_TIER_MODE", "serve")
    served = store._width_tier_lane(
        forward=forward, incumbent=(0.0, 1.0), incumbent_floor=0.0, incumbent_tiers=None,
        pooled=(0.0, 1.0), pooled_floor=0.0, tiered=(0.0, (1.0, 1.0, 0.6)),
    )
    assert served["state"] == "ADMITTED" and served["admitted"] is True
    # A candidate that buys the window by wrecking the bucket is refused.
    monkeypatch.setattr(store, "MAX_BUCKET_NLL_REGRESSION", -1.0)
    refused = store._width_tier_lane(
        forward=forward, incumbent=(0.0, 1.0), incumbent_floor=0.0, incumbent_tiers=None,
        pooled=(0.0, 1.0), pooled_floor=0.0, tiered=(0.0, (1.0, 1.0, 0.6)),
    )
    assert refused["state"] == "REFUSED_FORWARD"


def test_tiers_are_content_addressed_persisted_and_loaded(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "cw.db")
    tiers = {"boundaries_rungs": list(WIDTH_TIER_BOUNDARIES_RUNGS), "lambdas": [1.0, 0.8, 0.6]}
    plain = store.center_width_sha256(stage="P0", delta_c=0.1, width_lambda=0.75)
    tiered = store.center_width_sha256(stage="P0", delta_c=0.1, width_lambda=0.75, width_tiers=tiers)
    assert plain != tiered
    committed = store.persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.1, width_lambda=0.75, evidence={"schema": "t"},
        serving_day_count=9, width_tiers=tiers,
    )
    assert committed["state"] == "ACTIVATED" and committed["center_width_sha256"] == tiered
    assert store.load_center_width(conn, stage="P0")[:2] == (0.1, 0.75)
    assert store.load_center_width_tiers(conn, stage="P0") == tiers
    assert store.load_center_width_tiers(conn, stage="P0", table_sha256=tiered) == tiers
    assert store.load_center_width_tiers(conn, stage="P1") is None
    assert store.WIDTH_TIER_MODE == "shadow"


def test_every_serving_face_carries_the_tiers() -> None:
    """The floor's plumbing, mirrored: a face that applies the pair without
    the tiers would serve a distribution the fit never scored."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    read = lambda rel: (root / rel).read_text(encoding="utf-8")
    mat = read("bayes_basket_target/materialization.py")
    assert mat.count("center_width_tiers: Mapping[str, Any] | None = None,") == 4
    assert mat.count("center_width_tiers=center_width_tiers,") == 3
    assert '(center_width_tiers or {}).get("lambdas") or None' in mat
    assert "or center_width_tiers\n" in mat
    p1 = read("bayes_basket_target/p1_compiler.py")
    assert "center_width_tiers: Mapping[str, Any] | None = None," in p1
    assert "center_width_tiers=center_width_tiers," in p1
    worker = read("bayes_basket_target/production_worker.py")
    assert worker.count("load_center_width_tiers(") == 3
    ingress = read("bayes_basket_target/canonical_ingress.py")
    assert 'center_width_tiers=load_center_width_tiers(conn, stage="P1"),' in ingress
    p0 = read("bayes_basket_target/native_ensemble_p0.py")
    assert 'width_tiers = load_center_width_tiers(' in p0
    assert '(width_tiers or {}).get("lambdas") or None' in p0
    oos = read("bayes_basket_target/native_ensemble_oos.py")
    assert 'load_center_width_tiers(conn, stage="P0")' in oos
    assert '(center_width_tiers or {}).get("lambdas") or None' in oos
    ledger = read("scripts/paper_trading_ledger.py")
    assert '(entry.get("width_tiers") or {}).get("lambdas") or None' in ledger
