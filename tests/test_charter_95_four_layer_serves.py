"""Charter 95 step two, served: the chain stands in the ceiling's place at P1.

Operator ruling 2026-09-07 ("别影子了，直接上线").  The chain answers the
ceiling's question -- how much mass belongs above the bucket already holding
the day's high, and how the buckets above it are shaped -- from a regression
over the clock, the reports and the regional curve, instead of from a rate per
hours-left bin.  Measured walk-forward over 18096 samples, every day fitted on
strictly earlier days: 0.644 hit, -0.900 nat, 0.430 Brier, 0.577C centre MAE
against production's 0.632 / -0.962 / 0.448 / 0.616.

The translation stays off.  It corrects the model's level and production
already corrects that twice above -- the centre/width scalars and the band
table -- so serving it here would stack a third correction on two nobody
measured it against.

Three properties are pinned: the chain does move the served distribution (a
promotion that changed nothing would be a promotion that did not happen), only
one of the three claimants at that point ever speaks, and the switch is a real
way back.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import metar_four_layer as F  # noqa: E402
from bayes_basket_target.p1_compiler import compile_station_p1_packets  # noqa: E402
from tests.test_p1_four_layer_shadow import (  # noqa: E402
    HAZARD, REANCHOR, _four_reports, _root_with_intervals,
)
from tests.test_p1_trajectory_staged_cutover import (  # noqa: E402
    _REGISTRY, _promoted_artifact, _two_bucket_observations,
)

# A ceiling that would leave the mass where it is, so any move in the served
# distribution is the chain's and not the ceiling's.
CEILING = {
    "schema": "bayes_basket_target_metar_ceiling_v2",
    "trained_through": "2026-08-08",
    "bins": {
        str(index): {
            "hours_from": float(index) * 4.0,
            "hours_to": float(index + 1) * 4.0,
            "p_still_rose": 0.5,
            "sample_n": 400,
            "backed_off": False,
            "by_gap": {},
        }
        for index in range(6)
    },
}


def _compile(**overrides):
    arguments = {
        "predecessor": _root_with_intervals(),
        "observations": _four_reports(),
        "registry_rows": _REGISTRY,
        "compilation_at_utc": "2026-08-09T19:00:00Z",
        "trajectory_artifact": _promoted_artifact("AFTERNOON"),
        "soft_likelihood_policy": "ACTIVE",
        "ceiling_artifact": CEILING,
    }
    arguments.update(overrides)
    return compile_station_p1_packets(**arguments)


def _served(packets):
    return [packet["expected_envelope"]["probabilities"] for packet in packets]


def test_the_chain_takes_the_ceilings_place_and_moves_what_is_served() -> None:
    """A promotion that changed nothing would be a promotion that did not happen."""

    ceiling_only = _compile()
    chained = _compile(
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR})
    assert len(ceiling_only) == len(chained) >= 2
    assert _served(ceiling_only) != _served(chained), (
        "the chain is supposed to answer the ceiling's question differently"
    )
    for packet in chained:
        lineage = packet["expected_envelope"]["lineage"]
        receipt = json.loads(lineage.get("p1_four_layer_serving_json") or "{}")
        if receipt.get("applied") != "true":
            continue
        assert receipt["reanchor_enabled"] == "false", (
            "the translation is the layer production already does twice"
        )
        # Exactly one claimant speaks at that point.
        ceiling_receipt = json.loads(lineage.get("metar_ceiling_json") or "{}")
        assert ceiling_receipt.get("metar_ceiling_applied") != "true"
        break
    else:
        pytest.fail("the chain never spoke on any step")


def test_the_switch_is_a_real_way_back(monkeypatch) -> None:
    monkeypatch.setattr(F, "SERVE_ENABLED", False)
    import bayes_basket_target.materialization as M  # noqa: PLC0415

    monkeypatch.setattr(M, "FOUR_LAYER_SERVE_ENABLED", False)
    ceiling_only = _compile()
    switched_off = _compile(
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR})
    assert _served(ceiling_only) == _served(switched_off), (
        "with the switch off the ceiling serves exactly as it did before"
    )


def test_no_artifact_means_the_ceiling_serves_untouched() -> None:
    """The chain is silent where it cannot speak, and silence is the old path."""

    assert _served(_compile()) == _served(_compile(four_layer_artifacts=None))


def test_the_ruling_and_its_reading_ride_with_the_switch() -> None:
    assert F.SERVE_ENABLED is True
    source = (ROOT / "bayes_basket_target" / "metar_four_layer.py").read_text(
        encoding="utf-8")
    assert "PM_P1_FOUR_LAYER_SERVE" in source
    assert "0.577C centre MAE" in source or "0.577" in source
    materialization = (
        ROOT / "bayes_basket_target" / "materialization.py"
    ).read_text(encoding="utf-8")
    assert "reanchor_enabled=False," in materialization
    assert (
        "if ceiling is not None and not regional_spoke and not four_layer_spoke:"
        in materialization
    )


def test_a_broken_artifact_costs_the_chain_and_never_the_compile() -> None:
    """A layer that may raise at this point can stop the order chain.

    The shadow has carried this protection since it shipped, on the grounds
    that a measurement lane may not hold trading; a layer that graduated to
    serving does not get to drop it.  On a refusal the chain simply does not
    speak, the ceiling serves -- which is the pre-promotion path -- and the
    receipt names the refusal instead of swallowing it.
    """

    broken = {**HAZARD, "phase": {"hours_edges": [0.0], "bin_intercepts": []}}
    ceiling_only = _compile()
    with_broken = _compile(
        four_layer_artifacts={"hazard": broken, "reanchor": REANCHOR})
    assert len(with_broken) == len(ceiling_only) >= 2, "the compile survives"
    assert _served(with_broken) == _served(ceiling_only), (
        "a refused chain leaves exactly the distribution the ceiling served"
    )
    receipts = [
        json.loads(
            packet["expected_envelope"]["lineage"].get(
                "p1_four_layer_serving_json") or "{}")
        for packet in with_broken
    ]
    spoke = [r for r in receipts if r.get("applied") == "true"]
    assert not spoke, "a broken artifact must not price anything"
    assert any(r.get("refused") for r in receipts if r), (
        "the refusal is recorded, not swallowed"
    )


# A run over the fixture's own afternoon, knowable before its first decision.
KLGA_RUN = {
    "ensemble_system_id": "NCEP_NBM_POINT",
    "run_init_utc": "2026-08-09T12:00:00Z",
    "archive_query_at_utc": "2026-08-09T13:00:00Z",
    "knowable_at_utc": "2026-08-09T13:00:00Z",
    "hourly_c": {
        "2026-08-09T15:00:00Z": 22.0, "2026-08-09T16:00:00Z": 24.0,
        "2026-08-09T17:00:00Z": 25.5, "2026-08-09T18:00:00Z": 26.0,
        "2026-08-09T19:00:00Z": 25.0, "2026-08-09T20:00:00Z": 23.5,
    },
}


def _receipts(packets):
    out = []
    for packet in packets:
        raw = packet["expected_envelope"]["lineage"].get(
            "p1_four_layer_serving_json")
        if raw:
            out.append(json.loads(raw))
    return out


def test_the_receipt_says_which_lane_spoke_and_what_it_said() -> None:
    """Charter 99's rule, applied to this layer: a borrow is declared.

    Without these three fields a step the regional curve informed cannot be
    told from one where the fleet mean filled the feature in, and the only way
    to find out afterwards is to recompute the feature -- an audit
    reimplementing the rule it audits.
    """

    covered = _compile(
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR},
        regional_runs=[KLGA_RUN],
    )
    spoke = [r for r in _receipts(covered) if r.get("applied") == "true"]
    assert spoke, "the chain never spoke on any step"
    receipt = spoke[0]
    assert receipt["regional_system_id"] == "NCEP_NBM_POINT"
    assert receipt["regional_headroom_c"] != "NONE"
    # Headroom is the curve's remaining peak against what the day has
    # reported, so it is a signed number of degrees, not a temperature.
    assert -30.0 < float(receipt["regional_headroom_c"]) < 30.0


def test_a_step_no_regional_run_reached_says_so_rather_than_looking_served() -> None:
    plain = _compile(
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR})
    spoke = [r for r in _receipts(plain) if r.get("applied") == "true"]
    assert spoke
    for receipt in spoke:
        assert receipt["regional_system_id"] == "POOLED_FALLBACK"
        assert receipt["regional_headroom_c"] == "NONE"
        assert receipt["regional_live_gap_c"] == "NONE"


def test_declaring_the_provenance_moves_no_probability() -> None:
    """A receipt field is a receipt field; it may not price anything."""

    without = _compile(
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR})
    with_lane = _compile(
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR},
        regional_runs=[KLGA_RUN],
    )
    # The lane does move the served distribution -- that is the feature doing
    # its job -- so what is pinned here is the narrower claim: the receipt
    # keys are additions, and every other lineage value is what it was.
    assert len(without) == len(with_lane)
    for plain, laned in zip(without, with_lane):
        left = plain["expected_envelope"]["lineage"]
        right = laned["expected_envelope"]["lineage"]
        assert set(left) == set(right), "no new lineage keys, only new fields"
        for key in set(left) - {
            "p1_four_layer_serving_json", "p1_shadow_four_layer_json",
            "p1_shadow_reanchor_json", "predecessor_content_sha256",
            "predecessor_checkpoint_id",
        }:
            assert left[key] == right[key], key
