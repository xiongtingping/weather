"""The 2026-09-19 review of P1's joint observation likelihood (charter 150).

1. An open top ("33C or higher") got a finite upper edge of floor + one rung,
   so a day that climbed a rung past it excluded every bucket and P1 refused
   with ``hard_bound_exhausts_support`` (20 station-days, VILK 09-16 among
   them).  The open top's edge is None: unbounded, as P0/P0-M always read it.
2. The fitter scored alpha without the served zero floor.  It scores through
   ``materialization.floor_probabilities`` now, and its vectorised score is
   held equal to that row by row.
3. The alpha curve was validated on interleaved days (the 6th and 8th
   scoring the 5th and 7th).  It is walked forward now, each day on a curve
   fitted only on rows whose truth was committed before the day began, and
   the split's g trains only on truth committed before the held-out window.
4. Rung one learned "crossed a market bucket", the continuation "rounded
   warming >= 1C".  Both read the settled bucket the way serving does
   (``serving_label``), and a settled open top is right-censored.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest

from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.metar_trajectory import hard_bound_excluded_buckets
from bayes_basket_target.p1_compiler import _bucket_geometry_c, compile_station_p1_packets
from tests.test_bayes_basket_target_p1_bucket_geometry import F_INTERVALS, F_UPPER_LABELS, _root
from tests.test_bayes_basket_target_materialization import _envelope, _p0m_root
from tests.test_charter_150_p1_joint_likelihood import _artifact, _observation, _registry
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target.materialization import (
    PROBABILITY_MODEL_ZERO_FLOOR,
    floor_probabilities,
)

ROOT = Path(__file__).resolve().parents[1]


def _fitter():
    spec = importlib.util.spec_from_file_location(
        "_fit_p1_review_20260919", ROOT / "scripts" / "fit_p1_observation_likelihood.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _open_top_root() -> ProbabilityEnvelope:
    base = _p0m_root(_envelope())
    return ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "lineage": {
            **base.lineage,
            "bucket_intervals_c_json": json.dumps(
                {"37": [36.5, 37.5], "38": [37.5, 38.5], "39": [38.5, None]}
            ),
        },
    })


@pytest.mark.parametrize("high", [38.6, 39.5, 40.0, 44.0])
def test_a_day_past_the_open_top_keeps_the_top_bucket(high) -> None:
    root = _open_top_root()
    packets = compile_station_p1_packets(
        predecessor=root,
        observations=[_observation(root, "a", "2026-07-23T15:00:00Z", high)],
        registry_rows=_registry(root),
        compilation_at_utc="2026-07-23T15:00:00Z",
        observation_likelihood_artifact=_artifact(),
    )
    assert packets, "P1 refused a day above its open top"
    packet = packets[-1]
    assert packet["bucket_upper_bounds_c"]["39"] is None
    assert sorted(packet["hard_excluded_buckets"]) == ["37", "38"]
    envelope = packet["expected_envelope"]
    assert envelope["probabilities"]["39"] == pytest.approx(1.0)


def test_the_f_ladder_s_open_top_is_unbounded_too() -> None:
    _, uppers = _bucket_geometry_c(
        _root({"bucket_intervals_c_json": json.dumps(F_INTERVALS)}),
        unit="F", upper_by_bucket=F_UPPER_LABELS,
    )
    ordered = list(F_INTERVALS)
    top = ordered[-1]
    assert uppers[top] is None
    # Two rungs past the top's floor: everything but the top is out.
    high = F_INTERVALS[top][0] + 2 * 10.0 / 9.0
    assert hard_bound_excluded_buckets(ordered, uppers, high) == tuple(ordered[:-1])


def test_the_registry_ladder_s_open_top_is_unbounded_too() -> None:
    root = _p0m_root(_envelope())
    bare = ProbabilityEnvelope.from_mapping({
        **root.to_dict(),
        "lineage": {k: v for k, v in root.lineage.items() if k != "bucket_intervals_c_json"},
    })
    _, uppers = _bucket_geometry_c(
        bare, unit="C", upper_by_bucket={"37": 37.0, "38": 38.0, "39": None},
    )
    assert uppers == {"37": 38.0, "38": 39.0, "39": None}
    assert hard_bound_excluded_buckets(["37", "38", "39"], uppers, 45.0) == ("37", "38")


# -- 2. scored under the served floor ---------------------------------------


def test_the_vector_score_is_the_served_floor_row_by_row() -> None:
    fitter = _fitter()
    rng = np.random.default_rng(20260919)
    buckets = [str(b) for b in range(8)]
    priors, evidences, lives, truths, scalar = [], [], [], [], []
    alphas = rng.uniform(0.0, 3.0, 300)
    for alpha in alphas:
        raw = rng.dirichlet(np.full(8, 0.3))
        excluded = set(buckets[: int(rng.integers(0, 3))])
        prior = {b: float(v) for b, v in zip(buckets, raw)}
        evidence = {b: float(v) for b, v in zip(buckets, rng.uniform(0.0, 1.0, 8) ** 6)}
        truth = str(rng.choice([b for b in buckets if b not in excluded]))
        score = fitter._log_score(prior, evidence, excluded, truth, float(alpha))
        if score is None:
            continue
        scalar.append(score)
        priors.append([0.0 if b in excluded else prior[b] for b in buckets])
        evidences.append([evidence[b] for b in buckets])
        lives.append([b not in excluded for b in buckets])
        truths.append(buckets.index(truth))
    arrays = {
        "prior": np.array(priors), "evidence": np.array(evidences),
        "live": np.array(lives), "truth": np.array(truths),
    }
    mask = np.ones(len(truths), dtype=bool)
    vector = fitter._row_scores(arrays, alphas[: len(truths)], mask)
    assert np.allclose(vector, scalar, atol=1e-12)
    # The floor bites on most of these rows, as it did on 17,941 of 19,084.
    unfloored = [
        math.log(max(p[t] * e[t] ** a / sum(pp * ee ** a for pp, ee in zip(p, e)), 1e-300))
        for p, e, t, a in zip(priors, evidences, truths, alphas)
    ]
    assert sum(1 for s, u in zip(scalar, unfloored) if abs(s - u) > 1e-9) > len(scalar) // 2
    assert min(scalar) >= math.log(PROBABILITY_MODEL_ZERO_FLOOR / (1 + 8 * PROBABILITY_MODEL_ZERO_FLOOR))


def test_the_scalar_score_calls_the_served_floor() -> None:
    fitter = _fitter()
    prior = {"a": 0.5, "b": 0.4999, "c": 0.0001}
    evidence = {"a": 1.0, "b": 1.0, "c": 1.0}
    served, floored = floor_probabilities(prior)
    assert floored == ["c"]
    assert fitter._log_score(prior, evidence, set(), "c", 1.0) == pytest.approx(math.log(served["c"]))


# -- 4. both halves of g read the settled bucket as served -------------------


def _f_view() -> dict:
    root = _root({"bucket_intervals_c_json": json.dumps(F_INTERVALS)})
    centers, uppers = _bucket_geometry_c(root, unit="F", upper_by_bucket=F_UPPER_LABELS)
    return {
        "probabilities": dict(root.probabilities), "ordinals": dict(root.bucket_ordinals),
        "centers": centers, "uppers": uppers, "intervals": F_INTERVALS,
        "intervals_json": json.dumps(F_INTERVALS),
    }


@pytest.mark.parametrize(
    "high, truth, label",
    [
        # Crossed 67.5F at a third of a degree: a rise the rounding called 0.
        (19.5, 19.8, {"serving_rise": 1, "serving_rung": 1, "serving_censored": False}),
        # A full degree inside one 2F bucket: rounding called it a rise.
        (18.7, 19.7, {"serving_rise": 0, "serving_rung": None, "serving_censored": False}),
        # Settled in the open top: at least the top's rung, censored.
        (23.2, 27.0, {"serving_rise": 1, "serving_rung": 3, "serving_censored": True}),
        # Already in the open top: no bucket above to rise into.
        (26.0, 30.0, {"serving_rise": 0, "serving_rung": None, "serving_censored": False}),
    ],
)
def test_the_label_is_the_bucket_serving_reads(high, truth, label) -> None:
    fitter = _fitter()
    got = fitter.serving_label({"running_high_c": high, "truth_tmax_c": truth}, _f_view())
    assert got == label
    if label["serving_rise"]:
        # The same two functions serving calls say so.
        view = _f_view()
        ordered = sorted(view["ordinals"], key=view["ordinals"].get)
        running = next(b for b in ordered if view["uppers"][b] is None or view["uppers"][b] > high)
        truth_bucket = next(b for b in ordered if view["uppers"][b] is None or truth < view["uppers"][b])
        inc = OL.rise_increment_c(
            view["ordinals"][truth_bucket], view["ordinals"][running], view["centers"][truth_bucket], high,
        )
        assert OL.nearest_rung(inc, range(1, 13)) == label["serving_rung"]


def _riser(rung: int, censored: bool, rng) -> dict:
    return {
        "serving_rise": 1, "serving_rung": rung, "serving_censored": censored,
        "delta_c": rung, "station_day_weight": 1.0,
        **{name: float(rng.normal()) for name in OL.FULL_FEATURES},
    }


def test_a_censored_open_top_only_says_the_day_went_on() -> None:
    fitter = _fitter()
    rng = np.random.default_rng(5)
    base = [_riser(int(rng.integers(1, 6)), False, rng) for _ in range(900)]
    fallbacks = {name: 0.0 for name in OL.FULL_FEATURES}
    plain = fitter.fit_continuation(base, OL.FULL_FEATURES, fallbacks)
    censored = fitter.fit_continuation(base + [_riser(2, True, rng)], OL.FULL_FEATURES, fallbacks)
    stopped = fitter.fit_continuation(base + [_riser(2, False, rng)], OL.FULL_FEATURES, fallbacks)
    reached = lambda body, rung: body["rung_reached_rows"].get(str(rung), 0.0)
    # Censored at 2: it answers rung 2 (went on) and nothing past it.
    assert reached(censored, 2) == reached(plain, 2) + 1
    assert reached(censored, 3) == reached(plain, 3)
    assert censored["row_n"] == plain["row_n"] + 1
    # Uncensored at 2: it also answers rung 3 -- it stopped there.
    assert reached(stopped, 3) == reached(plain, 3) + 1
    assert stopped["row_n"] == plain["row_n"] + 2
    # A day that did not rise feeds rung one only.
    idle = {**_riser(1, False, rng), "serving_rise": 0, "serving_rung": None}
    assert fitter.fit_continuation(base + [idle], OL.FULL_FEATURES, fallbacks)["row_n"] == plain["row_n"]
    assert fitter.phase_outcome(idle) == 0.0 and fitter.phase_outcome(base[0]) == 1.0


# -- 3. walked forward, strictly as-of -----------------------------------------


def test_the_forward_curve_never_reads_a_truth_committed_after_its_day_began(monkeypatch) -> None:
    fitter = _fitter()
    from tests.test_charter_156_p1_smooth_clock import _synthetic_arrays

    arrays = _synthetic_arrays()
    masks = []
    monkeypatch.setattr(
        fitter, "fit_alpha_curve",
        lambda arr, mask: masks.append(mask.copy()) or [1.0] * len(OL.ALPHA_KNOTS_HOURS),
    )
    report = fitter.score_alpha_curve(arrays)
    days = sorted(report["forward_values_by_day"])
    assert len(masks) == len(days) + 1 and masks[-1].all()
    for day, mask in zip(days, masks):
        rows = arrays["day"] == day
        assert arrays["known_at"][mask].max() <= arrays["as_of"][rows].min()
        assert not (mask & rows).any()


def test_the_split_g_trains_only_on_truth_known_before_the_window() -> None:
    fitter = _fitter()
    day0 = 1788220800.0
    samples = [
        {"event_date": f"2026-09-{d + 1:02d}", "as_of_epoch": day0 + 86400.0 * d + 1800.0,
         "truth_known_epoch": day0 + 86400.0 * (d + 1) + 7200.0}
        for d in range(10)
    ]
    days = sorted({s["event_date"] for s in samples})
    cut, train, held, start = fitter.split_as_of(samples, days)
    assert cut == "2026-09-08" and start == samples[7]["as_of_epoch"]
    # 09-07's truth commits at 02:00 on the 8th, after the window's first report.
    assert [s["event_date"] for s in train] == days[:6]
    assert [s["event_date"] for s in held] == days[7:]
