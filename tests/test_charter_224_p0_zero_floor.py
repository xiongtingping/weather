"""Charter 224: P0 serves no bucket below the model-zero floor.

P0-M and P1 have floored every non-excluded bucket at
``PROBABILITY_MODEL_ZERO_FLOOR`` since the 2026-08-26 ruling; P0 never did, so
a truncated P0 tail served exactly zero (461 of 180,607 settled P0 envelopes,
21 station-days) and every P1 zero was a P0 root's.  The P0 compile now ends
with the same function, so one floor holds at every stage.
"""
from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

from bayes_basket_target import materialization, native_ensemble_p0, serving_arithmetic
from bayes_basket_target.native_ensemble_p0 import compile_native_ensemble_p0
from tests.test_native_ensemble_p0 import (  # noqa: F401 - autouse fixtures
    _contest_era_membership,
    _install_mixed_cycle_calibrations,
    _map_the_fixture_station_season,
    _mixed_packet,
)

ROOT = Path(__file__).resolve().parents[1]
FLOOR = materialization.PROBABILITY_MODEL_ZERO_FLOOR


def test_p0_uses_the_stages_one_floor():
    assert native_ensemble_p0.floor_probabilities is materialization.floor_probabilities
    assert FLOOR == 1e-3


def _calls(tree, name):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == name]


def test_the_floor_is_the_last_thing_the_served_vector_meets():
    tree = ast.parse(textwrap.dedent(inspect.getsource(compile_native_ensemble_p0)))
    layers = sorted(node.lineno for node in _calls(tree, "apply_served_table_layers"))
    floors = sorted(node.lineno for node in _calls(tree, "floor_probabilities"))
    assert len(layers) == 2 and len(floors) == 2
    # The served vector is floored right after its layers ...
    assert layers[0] < floors[0] < layers[1]
    # ... and the shadow's re-run of the same layers is floored too, so the
    # shadow compares like with like: the floor wraps the layers' result.
    shadow = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_served_layers_again")
    (wrapper,) = _calls(shadow, "floor_probabilities")
    assert _calls(wrapper.args[0], "apply_served_table_layers")


def test_the_fingerprint_covers_the_floor():
    assert ("bayes_basket_target.materialization", "floor_probabilities") in serving_arithmetic.closure("P0")


def test_a_compiled_p0_has_no_bucket_under_the_floor():
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    compiled = compile_native_ensemble_p0(
        conn, forward_packet=_mixed_packet(), decision_at_utc="2026-07-01T13:00:00Z",
    )
    envelope = compiled["packet"]["native_probability_envelope"]
    probabilities = envelope["probabilities"]
    assert sum(probabilities.values()) == pytest.approx(1.0, abs=1e-12)
    # Lifted to the floor, then renormalised: a lifted bucket lands a hair
    # under it (P1 behaves the same), never at zero.
    assert min(probabilities.values()) > FLOOR * 0.99
    served = json.loads(compiled["forward_prediction"]["probabilities_json"])
    assert served == probabilities
    floor = envelope["lineage"].get("probability_floor_json")
    if floor is not None:
        receipt = json.loads(floor)
        assert receipt["schema"] == "probability_model_zero_floor_v1"
        assert receipt["epsilon"] == FLOOR
        assert receipt["floored_buckets"]


def test_a_vector_already_above_the_floor_is_returned_unchanged():
    vector = {"a": 0.5, "b": 0.3, "c": 0.2}
    assert materialization.floor_probabilities(vector) == (vector, [])


def test_zero_buckets_are_lifted_and_the_rest_renormalised():
    lifted, floored = materialization.floor_probabilities({"a": 0.0, "b": 0.6, "c": 0.4})
    assert floored == ["a"]
    assert min(lifted.values()) > 0.0
    assert sum(lifted.values()) == pytest.approx(1.0, abs=1e-12)
    assert lifted["b"] / lifted["c"] == pytest.approx(1.5)


def test_charter_224_records_the_rule():
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n224. ", 1)[1].split("\n", 1)[0]
    for needle in ("floor_probabilities", "PROBABILITY_MODEL_ZERO_FLOOR", "不劣", "v17", "v19", "v28", "裁决人：用户"):
        assert needle in clause
