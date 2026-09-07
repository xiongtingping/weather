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
